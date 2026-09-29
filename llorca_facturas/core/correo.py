"""
CORREO SALIENTE: la aplicación envía los correos que antes preparaba y enviaba una persona.

- Todo correo pasa por una BANDEJA DE SALIDA (`correo_salida`) con su origen (reclamación de retención, petición de
  oferta, autorización de facturación a un subcontratista, aviso de pago, documentación CAE caducada…).
- Por tipo de correo se decide si sale solo o si alguien debe aprobarlo antes (por defecto: aprobación). Nadie aprueba
  su propio correo si así se configura (cuatro ojos).
- Un proceso de fondo envía los aprobados por SMTP (usuario y contraseña, o Microsoft 365 con OAuth2), con reintentos.
- Modo PRUEBA: no se envía nada; los correos se generan como .eml en `data/correo_prueba/` para revisarlos.
- Nunca se envía dos veces el mismo correo: cada uno tiene su Message-ID y su estado; los reintentos solo tocan los fallidos.
- Todo queda auditado: quién lo creó, quién lo aprobó, cuándo salió y la respuesta del servidor.
"""
from __future__ import annotations

import json
import mimetypes
import re
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, formatdate
from pathlib import Path

from . import db
from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS correo_salida (
    id INTEGER PRIMARY KEY, tipo TEXT NOT NULL, para TEXT NOT NULL, cc TEXT, asunto TEXT NOT NULL, cuerpo TEXT NOT NULL,
    adjuntos TEXT, origen TEXT, origen_id INTEGER, obra_id INTEGER,
    estado TEXT NOT NULL DEFAULT 'pendiente_aprobacion', intentos INTEGER DEFAULT 0, proximo_intento TEXT,
    creado_por TEXT, creado_en TEXT, aprobado_por TEXT, aprobado_en TEXT, enviado_en TEXT, message_id TEXT,
    error TEXT, respuesta TEXT, cancelado_por TEXT);
CREATE INDEX IF NOT EXISTS ix_cs_estado ON correo_salida(estado);
CREATE INDEX IF NOT EXISTS ix_cs_origen ON correo_salida(origen, origen_id);
"""
TIPOS = {
    "reclamacion_retencion": "Reclamación de retención de garantía al cliente",
    "reclamacion_cobro": "Recordatorio de cobro vencido",
    "peticion_oferta": "Petición de oferta a industriales",
    "autorizacion_facturacion": "Certificación aprobada al subcontratista (autorización de facturación)",
    "aviso_pago": "Aviso de pago a proveedor (remesa ejecutada)",
    "cae": "Documentación CAE caducada o pendiente",
    "planos": "Distribución de planos y fichas técnicas",
    "otro": "Otro",
}
ESTADOS = {"pendiente_aprobacion": "Pendiente de aprobar", "aprobado": "Aprobado (en cola)", "enviado": "Enviado",
           "error": "Error (se reintenta)", "fallido": "Fallido (sin más reintentos)", "cancelado": "Cancelado"}
MAX_INTENTOS = 5
RE_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def init(con):
    con.executescript(SCHEMA)
    for col in ("email TEXT", "email_administracion TEXT"):
        try:
            con.execute(f"ALTER TABLE proveedores ADD COLUMN {col}")
        except Exception:
            pass
    con.commit()


def config(con) -> dict:
    g = lambda k, d="": db.get_setting(con, "smtp_" + k, d)  # noqa: E731
    return {"modo": g("modo", "desactivado"),        # desactivado | prueba | real
            "host": g("host"), "puerto": int(g("puerto", "587") or 587), "seguridad": g("seguridad", "starttls"),
            "usuario": g("usuario"), "auth": g("auth", "clave"), "tenant": g("tenant"), "client_id": g("client_id"),
            "remitente": g("remitente"), "nombre": g("nombre", "Llorca Group"), "responder_a": g("responder_a"),
            "copia_oculta": g("copia_oculta"), "firma": g("firma", "\n--\nLlorca Group Hispania, S.L."),
            "auto": {t for t in g("auto_tipos", "aviso_pago").split(",") if t},
            "cuatro_ojos": g("cuatro_ojos", "0") == "1", "por_vuelta": int(g("por_vuelta", "30") or 30)}


def carpeta_prueba() -> Path:
    p = DATA_DIR / "correo_prueba"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _lista(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        v = ",".join(v)
    return [x.strip() for x in re.split(r"[,;]", v) if x.strip()]


def validar_direcciones(v) -> list[str]:
    malas = [x for x in _lista(v) if not RE_EMAIL.match(x)]
    return malas


# ============================================================================ bandeja de salida
def preparar(con, tipo: str, para, asunto: str, cuerpo: str, usuario: str, adjuntos: list[str | Path] | None = None,
             cc=None, origen: str = "", origen_id: int | None = None, obra_id: int | None = None, evitar_duplicado: bool = True) -> int:
    """Deja un correo en la bandeja. Sale solo si su tipo está configurado como automático; si no, espera aprobación."""
    if tipo not in TIPOS:
        raise ValueError(f"Tipo de correo desconocido: {tipo}")
    dest = _lista(para)
    if not dest:
        raise ValueError("Falta el destinatario.")
    malas = validar_direcciones(dest) + validar_direcciones(cc)
    if malas:
        raise ValueError("Direcciones no válidas: " + ", ".join(malas))
    adj = [str(a) for a in (adjuntos or [])]
    for a in adj:
        if not Path(a).exists():
            raise ValueError(f"No existe el adjunto {a}")
    if evitar_duplicado and origen and origen_id is not None:
        ya = db.one(con, "SELECT id FROM correo_salida WHERE tipo=? AND origen=? AND origen_id=? AND estado NOT IN ('cancelado','fallido')",
                    (tipo, origen, origen_id))
        if ya:
            return ya["id"]
    cfg = config(con)
    estado = "aprobado" if tipo in cfg["auto"] else "pendiente_aprobacion"
    with db.tx(con):
        cur = con.execute("""INSERT INTO correo_salida (tipo, para, cc, asunto, cuerpo, adjuntos, origen, origen_id, obra_id, estado,
                             creado_por, creado_en, aprobado_por, aprobado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (tipo, ", ".join(dest), ", ".join(_lista(cc)), asunto.strip(), cuerpo, json.dumps(adj, ensure_ascii=False), origen,
                           origen_id, obra_id, estado, usuario, db.now_iso(),
                           "automático" if estado == "aprobado" else None, db.now_iso() if estado == "aprobado" else None))
        db.audit(con, usuario, "correo_preparado", "correo", cur.lastrowid, {"tipo": tipo, "para": dest, "asunto": asunto[:120]})
    return cur.lastrowid


def aprobar(con, cid: int, usuario: str, rol: str, cambios: dict | None = None) -> None:
    c = db.one(con, "SELECT * FROM correo_salida WHERE id=?", (cid,))
    if not c or c["estado"] not in ("pendiente_aprobacion", "error"):
        raise ValueError("Ese correo no está pendiente de aprobar.")
    if rol not in ("admin", "gestor", "direccion", "jefe_obra", "tecnico"):
        raise ValueError("Su cargo no puede aprobar el envío de correos.")
    if config(con)["cuatro_ojos"] and c["creado_por"] == usuario:
        raise ValueError("Cuatro ojos: el correo lo debe aprobar una persona distinta de quien lo preparó.")
    cambios = cambios or {}
    if "para" in cambios and validar_direcciones(cambios["para"]):
        raise ValueError("Direcciones no válidas: " + ", ".join(validar_direcciones(cambios["para"])))
    with db.tx(con):
        con.execute("""UPDATE correo_salida SET para=COALESCE(?, para), asunto=COALESCE(?, asunto), cuerpo=COALESCE(?, cuerpo),
                       estado='aprobado', aprobado_por=?, aprobado_en=?, intentos=0, proximo_intento=NULL, error=NULL WHERE id=?""",
                    (cambios.get("para"), cambios.get("asunto"), cambios.get("cuerpo"), usuario, db.now_iso(), cid))
        db.audit(con, usuario, "correo_aprobado", "correo", cid, {"editado": sorted(cambios)})


def cancelar(con, cid: int, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE correo_salida SET estado='cancelado', cancelado_por=? WHERE id=? AND estado<>'enviado'", (usuario, cid))
        db.audit(con, usuario, "correo_cancelado", "correo", cid, None)


def bandeja(con, estados: list[str] | None = None, limite: int = 300) -> list[dict]:
    q, p = "SELECT * FROM correo_salida", []
    if estados:
        q += f" WHERE estado IN ({','.join('?' * len(estados))})"
        p = list(estados)
    return db.rows(con, q + " ORDER BY id DESC LIMIT ?", p + [limite])


# ============================================================================ envío
def construir(con, c: dict, cfg: dict) -> EmailMessage:
    m = EmailMessage()
    m["From"] = formataddr((cfg["nombre"], cfg["remitente"] or cfg["usuario"]))
    m["To"] = c["para"]
    if c.get("cc"):
        m["Cc"] = c["cc"]
    if cfg["responder_a"]:
        m["Reply-To"] = cfg["responder_a"]
    m["Subject"] = c["asunto"]
    m["Date"] = formatdate(localtime=True)
    dominio = (cfg["remitente"] or cfg["usuario"] or "llorca.local").split("@")[-1]
    m["Message-ID"] = c.get("message_id") or make_msgid(idstring=f"llorca{c['id']}", domain=dominio)
    m["X-Llorca-Correo"] = str(c["id"])
    m.set_content(c["cuerpo"].rstrip() + ("\n" + cfg["firma"] if cfg["firma"] and cfg["firma"].strip() not in c["cuerpo"] else ""))
    for a in json.loads(c.get("adjuntos") or "[]"):
        p = Path(a)
        tipo = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        mt, st = tipo.split("/", 1)
        nombre = p.name
        m.add_attachment(p.read_bytes(), maintype=mt, subtype=st, filename=nombre)
    return m


def _smtp(con, cfg: dict):
    from . import credenciales
    if not cfg["host"]:
        raise ValueError("Configure el servidor de correo saliente (SMTP) en Configuración → Correo saliente.")
    ctx = ssl.create_default_context()
    if cfg["seguridad"] == "ssl":
        s = smtplib.SMTP_SSL(cfg["host"], cfg["puerto"], timeout=60, context=ctx)
    else:
        s = smtplib.SMTP(cfg["host"], cfg["puerto"], timeout=60)
        s.ehlo()
        if cfg["seguridad"] == "starttls":
            s.starttls(context=ctx)
            s.ehlo()
    if cfg["auth"] == "m365":
        tok = credenciales.token_m365(cfg["tenant"], cfg["client_id"], credenciales.leer(con, "smtp_client_secret"))
        s.auth("XOAUTH2", lambda challenge=None: credenciales.cadena_xoauth2(cfg["usuario"], tok), initial_response_ok=True)
    elif cfg["auth"] == "clave":
        clave = credenciales.leer(con, "smtp_clave")
        if cfg["usuario"]:
            if not clave:
                raise ValueError("Falta la contraseña SMTP (Configuración → Correo saliente, o variable LLORCA_SMTP_CLAVE).")
            s.login(cfg["usuario"], clave)
    return s


def probar_conexion(con, cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or config(con)
    try:
        s = _smtp(con, cfg)
        s.noop()
        s.quit()
        return True, f"Conexión SMTP correcta con {cfg['host']}:{cfg['puerto']}."
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def enviar_pendientes(con, cfg: dict | None = None) -> str:
    cfg = cfg or config(con)
    if cfg["modo"] == "desactivado":
        return "Envío desactivado."
    ahora = db.now_iso()
    cola = db.rows(con, """SELECT * FROM correo_salida WHERE (estado='aprobado' OR (estado='error' AND intentos<?))
                           AND (proximo_intento IS NULL OR proximo_intento<=?) ORDER BY id LIMIT ?""", (MAX_INTENTOS, ahora, cfg["por_vuelta"]))
    if not cola:
        return "Nada que enviar."
    enviados = errores = 0
    s = None
    try:
        for c in cola:
            try:
                m = construir(con, c, cfg)
                if cfg["modo"] == "prueba":
                    (carpeta_prueba() / f"correo_{c['id']:06d}.eml").write_bytes(bytes(m))
                    resp = f"modo prueba: guardado en {carpeta_prueba()}"
                else:
                    if s is None:
                        s = _smtp(con, cfg)
                    dest = _lista(c["para"]) + _lista(c.get("cc")) + _lista(cfg["copia_oculta"])
                    rechazados = s.send_message(m, to_addrs=dest)
                    resp = "aceptado" + (f"; rechazados: {rechazados}" if rechazados else "")
                con.execute("UPDATE correo_salida SET estado='enviado', enviado_en=?, message_id=?, respuesta=?, error=NULL WHERE id=?",
                            (db.now_iso(), m["Message-ID"], resp, c["id"]))
                db.audit(con, "correo automático", "correo_enviado", "correo", c["id"], {"para": c["para"], "modo": cfg["modo"]})
                con.commit()
                enviados += 1
            except (smtplib.SMTPServerDisconnected, ConnectionError, OSError) as e:
                s = None
                _fallo(con, c, e)
                errores += 1
            except Exception as e:  # noqa: BLE001
                _fallo(con, c, e)
                errores += 1
    finally:
        if s is not None:
            try:
                s.quit()
            except Exception:
                pass
    return f"{enviados} enviado(s)" + (f", {errores} con error" if errores else "") + (" (modo prueba)" if cfg["modo"] == "prueba" else "")


def _fallo(con, c: dict, e: Exception) -> None:
    intentos = (c.get("intentos") or 0) + 1
    espera = timedelta(minutes=5 * 2 ** (intentos - 1))          # 5, 10, 20, 40 min…
    con.execute("UPDATE correo_salida SET estado=?, intentos=?, error=?, proximo_intento=? WHERE id=?",
                ("fallido" if intentos >= MAX_INTENTOS else "error", intentos, f"{type(e).__name__}: {e}"[:500],
                 (datetime.now() + espera).isoformat(timespec="seconds"), c["id"]))
    con.commit()


def tareas():
    from .planificador import Tarea

    def toca(con, ultima, ahora):
        if config(con)["modo"] == "desactivado":
            return False
        return ultima is None or ahora - ultima >= timedelta(minutes=1)
    return [Tarea("correo", "Envío de correos aprobados", toca, enviar_pendientes)]


# ============================================================================ productores
def email_proveedor(con, proveedor_id: int | None) -> str:
    if not proveedor_id:
        return ""
    try:
        r = db.one(con, "SELECT email, email_administracion FROM proveedores WHERE id=?", (proveedor_id,))
        if r and (r.get("email_administracion") or r.get("email")):
            return r.get("email_administracion") or r["email"]
    except Exception:
        pass
    r = db.one(con, "SELECT email FROM buzon_remitentes WHERE proveedor_id=? ORDER BY veces DESC LIMIT 1", (proveedor_id,))
    return r["email"] if r else ""


def avisos_de_remesa(con, rid: int, usuario: str) -> int:
    """Al confirmar una remesa: un aviso de pago por proveedor (solo si se conoce su correo)."""
    from .money import fmt_eur
    from decimal import Decimal
    rem = db.one(con, "SELECT * FROM remesas WHERE id=?", (rid,))
    grupos: dict[int, list[dict]] = {}
    for r in db.rows(con, """SELECT ri.importe_cents, ri.iban, d.numero, d.fecha, d.proveedor_id, COALESCE(p.nombre, d.emisor_nombre) prov
                             FROM remesa_items ri JOIN documentos d ON d.id=ri.documento_id LEFT JOIN proveedores p ON p.id=d.proveedor_id
                             WHERE ri.remesa_id=?""", (rid,)):
        if r["proveedor_id"]:
            grupos.setdefault(r["proveedor_id"], []).append(r)
    n = 0
    for pid, filas in grupos.items():
        para = email_proveedor(con, pid)
        if not para:
            continue
        total = sum(Decimal(f["importe_cents"]) for f in filas) / 100
        det = "\n".join(f"  - Factura {f['numero']} de {f['fecha']}: {fmt_eur(Decimal(f['importe_cents']) / 100)}" for f in filas)
        cuerpo = (f"Estimados señores de {filas[0]['prov']}:\n\nLes informamos de que con fecha {rem['fecha_ejecucion']} hemos ordenado "
                  f"una transferencia de {fmt_eur(total)} a su cuenta terminada en {str(filas[0]['iban'] or '')[-4:]}, "
                  f"correspondiente a:\n\n{det}\n\nUn saludo.")
        preparar(con, "aviso_pago", para, f"Aviso de pago · {fmt_eur(total)} · {rem['referencia']}", cuerpo, usuario,
                 origen=f"remesa:{rid}", origen_id=pid)
        n += 1
    return n
