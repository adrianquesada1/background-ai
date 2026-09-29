"""
ESCRITURA DIRECTA EN SIS por API (asientos de las facturas recibidas aprobadas).

Hasta ahora los asientos se exportaban a Excel/CSV y se importaban en SIS a mano. Este conector los envía solos:

- Cola `sis_envios`: cada factura aprobada (desde la fecha de arranque configurada, para no duplicar lo ya contabilizado)
  entra en la cola con su asiento cuadrado (gasto, IVA, ISP, IRPF, retención y proveedor).
- Envío por REST (JSON) con la autenticación que admita la instalación de SIS: token Bearer, usuario/contraseña (Basic)
  o clave en cabecera. La URL, la ruta y los nombres de los campos son configurables (mapeo), porque cada instalación de
  SIS expone su API a su manera.
- Idempotente: cada envío lleva una referencia externa única (`LLORCA-<id>-<huella>`) en el cuerpo y en la cabecera
  `Idempotency-Key`; si SIS responde que ya existe (409), se da por contabilizado. Nunca se envía dos veces.
- Si la factura cambia DESPUÉS de haberse enviado (se reabre, se corrige un importe…), el envío pasa a «desfasado» y
  aparece en alertas para que Administración ajuste el asiento en SIS: el sistema no borra ni modifica asientos en SIS
  por su cuenta.
- Modo PRUEBA: genera exactamente lo que se enviaría (en `data/sis_prueba/`) sin llamar a SIS, para validarlo con el
  proveedor de SIS antes de activar el modo real.
- Reintentos con espera creciente; tras 6 fallos queda «fallido» para revisión. El Excel/CSV de asientos sigue disponible.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from . import db, asientos
from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS sis_envios (
    id INTEGER PRIMARY KEY, documento_id INTEGER UNIQUE NOT NULL, referencia TEXT, huella TEXT, payload TEXT,
    estado TEXT NOT NULL DEFAULT 'pendiente', intentos INTEGER DEFAULT 0, proximo_intento TEXT,
    id_sis TEXT, respuesta TEXT, error TEXT, creado_en TEXT, enviado_en TEXT, revisado_por TEXT, revisado_en TEXT);
CREATE INDEX IF NOT EXISTS ix_sis_estado ON sis_envios(estado);
"""
ESTADOS = {"pendiente": "Pendiente de enviar", "enviado": "Contabilizado en SIS", "error": "Error (se reintenta)",
           "fallido": "Fallido (revisar)", "desfasado": "Cambió tras enviarse (ajustar en SIS)", "omitido": "Omitido a mano"}
MAX_INTENTOS = 6
MAPEO_DEFECTO = {"empresa": "empresa", "diario": "diario", "fecha": "fecha", "documento": "documento", "concepto": "concepto",
                 "referencia": "referencia_externa", "lineas": "apuntes", "cuenta": "cuenta", "debe": "debe", "haber": "haber",
                 "nif": "nif", "centro_coste": "centro_coste", "adjunto": "adjunto"}


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def config(con) -> dict:
    g = lambda k, d="": db.get_setting(con, "sis_" + k, d)  # noqa: E731
    try:
        mapeo = {**MAPEO_DEFECTO, **json.loads(g("mapeo", "{}") or "{}")}
    except json.JSONDecodeError:
        mapeo = dict(MAPEO_DEFECTO)
    return {"modo": g("modo", "desactivado"), "url": g("url").rstrip("/"), "ruta": g("ruta", "/api/asientos") or "/api/asientos",
            "auth": g("auth", "bearer"), "usuario": g("usuario"), "cabecera_clave": g("cabecera_clave", "X-API-Key"),
            "empresa": g("empresa"), "diario": g("diario", "COMPRAS"), "desde": g("desde"), "campo_id": g("campo_id", "id"),
            "adjuntar_pdf": g("adjuntar_pdf", "0") == "1", "minutos": int(g("minutos", "10") or 10),
            "verificar_tls": g("verificar_tls", "1") == "1", "mapeo": mapeo}


def carpeta_prueba() -> Path:
    p = DATA_DIR / "sis_prueba"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ============================================================================ asiento → cuerpo JSON
def _asiento_doc(con, doc_id: int) -> list[dict]:
    return asientos.proponer(con, doc_ids=[doc_id])


def huella(filas: list[dict]) -> str:
    base = [(f["cuenta"], str(f["debe"]), str(f["haber"]), f.get("fecha"), f.get("documento"), f.get("nif")) for f in filas]
    return hashlib.sha256(json.dumps(base, default=str).encode()).hexdigest()[:12]


def construir_payload(con, doc_id: int, cfg: dict | None = None) -> tuple[dict, str, str]:
    cfg = cfg or config(con)
    filas = _asiento_doc(con, doc_id)
    if not filas:
        raise ValueError("La factura no está aprobada o no genera asiento.")
    if asientos.cuadran(filas):
        raise ValueError("El asiento no cuadra (debe ≠ haber): no se envía.")
    h = huella(filas)
    ref = f"LLORCA-{doc_id}-{h}"
    m = cfg["mapeo"]
    f0 = filas[0]
    cuerpo = {m["empresa"]: cfg["empresa"], m["diario"]: cfg["diario"], m["fecha"]: f0["fecha"], m["documento"]: f0["documento"],
              m["concepto"]: f0["concepto"], m["referencia"]: ref,
              m["lineas"]: [{m["cuenta"]: f["cuenta"], m["debe"]: f"{Decimal(f['debe']):.2f}", m["haber"]: f"{Decimal(f['haber']):.2f}",
                             m["concepto"]: f["concepto"], m["nif"]: f.get("nif"), m["centro_coste"]: f.get("centro_coste")} for f in filas]}
    if cfg["adjuntar_pdf"] and f0.get("pdf") and Path(f0["pdf"]).exists():
        cuerpo[m["adjunto"]] = {"nombre": f"{f0['documento'] or doc_id}.pdf",
                                "contenido_base64": base64.b64encode(Path(f0["pdf"]).read_bytes()).decode()}
    return cuerpo, ref, h


# ============================================================================ cola
def sincronizar_cola(con, cfg: dict | None = None) -> dict:
    """Añade a la cola las facturas aprobadas nuevas y detecta las ya enviadas que han cambiado."""
    cfg = cfg or config(con)
    desde = cfg["desde"] or date.today().isoformat()
    nuevas = desfasadas = 0
    aprobadas = db.rows(con, """SELECT id FROM documentos WHERE estado='aprobada' AND tipo_documento IN ('factura','abono','anticipo')
                                AND COALESCE(substr(aprobado_en,1,10), fecha) >= ? AND id NOT IN (SELECT documento_id FROM sis_envios)""", (desde,))
    for d in aprobadas:
        con.execute("INSERT OR IGNORE INTO sis_envios (documento_id, estado, creado_en) VALUES (?, 'pendiente', ?)", (d["id"], db.now_iso()))
        nuevas += 1
    for e in db.rows(con, """SELECT s.id, s.documento_id, s.huella, d.estado FROM sis_envios s JOIN documentos d ON d.id=s.documento_id
                             WHERE s.estado='enviado'"""):
        filas = _asiento_doc(con, e["documento_id"]) if e["estado"] == "aprobada" else []
        if not filas or huella(filas) != e["huella"]:
            con.execute("UPDATE sis_envios SET estado='desfasado', error=? WHERE id=?",
                        ("La factura ya no está aprobada" if not filas else "Los importes o cuentas cambiaron tras contabilizarla", e["id"]))
            desfasadas += 1
    # pendientes cuya factura dejó de estar aprobada antes de salir: se quitan de la cola
    con.execute("""DELETE FROM sis_envios WHERE estado IN ('pendiente','error') AND documento_id IN
                   (SELECT id FROM documentos WHERE estado<>'aprobada')""")
    con.commit()
    return {"nuevas": nuevas, "desfasadas": desfasadas}


def _peticion(con, cfg: dict, cuerpo: dict, ref: str):
    import requests
    from . import credenciales
    cab = {"Content-Type": "application/json", "Accept": "application/json", "Idempotency-Key": ref}
    auth = None
    secreto = credenciales.leer(con, "sis_clave")
    if cfg["auth"] == "bearer":
        cab["Authorization"] = f"Bearer {secreto}"
    elif cfg["auth"] == "basic":
        auth = (cfg["usuario"], secreto)
    elif cfg["auth"] == "cabecera":
        cab[cfg["cabecera_clave"]] = secreto
    return requests.post(cfg["url"] + "/" + cfg["ruta"].lstrip("/"), json=cuerpo, headers=cab, auth=auth, timeout=60,
                         verify=cfg["verificar_tls"])


def enviar(con, cfg: dict | None = None, limite: int = 50) -> str:
    cfg = cfg or config(con)
    if cfg["modo"] == "desactivado":
        return "Integración con SIS desactivada."
    if cfg["modo"] == "real" and not cfg["url"]:
        raise ValueError("Falta la dirección de la API de SIS.")
    s = sincronizar_cola(con, cfg)
    ahora = db.now_iso()
    cola = db.rows(con, """SELECT * FROM sis_envios WHERE (estado='pendiente' OR (estado='error' AND intentos<?))
                           AND (proximo_intento IS NULL OR proximo_intento<=?) ORDER BY id LIMIT ?""", (MAX_INTENTOS, ahora, limite))
    ok = err = 0
    for e in cola:
        try:
            cuerpo, ref, h = construir_payload(con, e["documento_id"], cfg)
            if cfg["modo"] == "prueba":
                (carpeta_prueba() / f"{ref}.json").write_text(json.dumps(cuerpo, ensure_ascii=False, indent=1), encoding="utf-8")
                id_sis, resp = None, "modo prueba: no enviado"
                estado = "pendiente"            # en prueba no se da por contabilizado
            else:
                r = _peticion(con, cfg, cuerpo, ref)
                if r.status_code == 409:
                    id_sis, resp = None, "ya existía en SIS (409)"
                elif 200 <= r.status_code < 300:
                    try:
                        j = r.json()
                        id_sis = str(j.get(cfg["campo_id"])) if isinstance(j, dict) and j.get(cfg["campo_id"]) is not None else None
                    except ValueError:
                        id_sis = None
                    resp = r.text[:500]
                else:
                    raise RuntimeError(f"SIS respondió {r.status_code}: {r.text[:300]}")
                estado = "enviado"
            con.execute("""UPDATE sis_envios SET estado=?, referencia=?, huella=?, payload=?, id_sis=?, respuesta=?, error=NULL,
                           enviado_en=CASE WHEN ?='enviado' THEN ? ELSE enviado_en END WHERE id=?""",
                        (estado, ref, h, json.dumps({k: v for k, v in cuerpo.items() if k != cfg["mapeo"]["adjunto"]}, ensure_ascii=False),
                         id_sis, resp, estado, db.now_iso(), e["id"]))
            if estado == "enviado":
                db.audit(con, "SIS", "asiento_enviado_sis", "documento", e["documento_id"], {"referencia": ref, "id_sis": id_sis})
            con.commit()
            ok += 1
        except Exception as ex:  # noqa: BLE001
            con.rollback()
            n = (e["intentos"] or 0) + 1
            con.execute("UPDATE sis_envios SET estado=?, intentos=?, error=?, proximo_intento=? WHERE id=?",
                        ("fallido" if n >= MAX_INTENTOS else "error", n, f"{type(ex).__name__}: {ex}"[:500],
                         (datetime.now() + timedelta(minutes=10 * 2 ** (n - 1))).isoformat(timespec="seconds"), e["id"]))
            con.commit()
            err += 1
    modo = " (modo prueba: revise data/sis_prueba)" if cfg["modo"] == "prueba" else ""
    return f"{s['nuevas']} nueva(s) en cola, {ok} {'generado(s)' if cfg['modo'] == 'prueba' else 'contabilizado(s)'}, {err} error(es)" + \
        (f", {s['desfasadas']} desfasada(s)" if s["desfasadas"] else "") + modo


def marcar(con, envio_id: int, estado: str, usuario: str, nota: str = "") -> None:
    """Revisión manual: «omitido» (ya estaba en SIS) o «enviado» tras ajustar a mano un desfasado."""
    if estado not in ("omitido", "enviado", "pendiente"):
        raise ValueError("Estado no permitido.")
    e = db.one(con, "SELECT * FROM sis_envios WHERE id=?", (envio_id,))
    h = e["huella"]
    if estado == "enviado":
        filas = _asiento_doc(con, e["documento_id"])
        h = huella(filas) if filas else h
    with db.tx(con):
        con.execute("UPDATE sis_envios SET estado=?, huella=?, revisado_por=?, revisado_en=?, error=?, intentos=0, proximo_intento=NULL WHERE id=?",
                    (estado, h, usuario, db.now_iso(), nota or None, envio_id))
        db.audit(con, usuario, "sis_revision_manual", "documento", e["documento_id"], {"estado": estado, "nota": nota})


def probar_conexion(con, cfg: dict | None = None) -> tuple[bool, str]:
    import requests
    cfg = cfg or config(con)
    if not cfg["url"]:
        return False, "Falta la dirección de la API."
    try:
        r = requests.get(cfg["url"], timeout=15, verify=cfg["verificar_tls"])
        return r.status_code < 500, f"SIS responde en {cfg['url']} (HTTP {r.status_code})."
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def resumen(con) -> dict:
    return {r["estado"]: r["n"] for r in db.rows(con, "SELECT estado, COUNT(*) n FROM sis_envios GROUP BY estado")}


def tareas():
    from .planificador import Tarea

    def toca(con, ultima, ahora):
        cfg = config(con)
        return cfg["modo"] != "desactivado" and (ultima is None or ahora - ultima >= timedelta(minutes=max(1, cfg["minutos"])))
    return [Tarea("sis", "Envío de asientos a SIS", toca, enviar)]
