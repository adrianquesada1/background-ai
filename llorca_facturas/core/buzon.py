"""
BUZÓN DE FACTURAS: lectura automática del correo (IMAP).

Cada pocos minutos (configurable) se revisa la carpeta del buzón de facturas:
1. Se descargan los correos nuevos sin marcarlos como leídos hasta haberlos procesado (si algo falla, se reintenta).
2. De cada correo se sacan los adjuntos: PDF, fotos y escaneos, ZIP (también anidados) y correos reenviados (.eml/.msg).
   Logotipos y firmas se descartan.
3. Cada PDF se CLASIFICA antes de entrar: solo los que se identifican como factura (o abono / rectificativa) pasan al
   circuito. Presupuestos, ofertas, albaranes, proformas, certificaciones a cliente, nóminas… se quedan apartados con el
   motivo, y se pueden importar con un clic si el clasificador se equivocó. Lo dudoso también queda apartado para decidir.
4. Las facturas se registran igual que una subida manual (huella SHA-256: el mismo PDF nunca entra dos veces, y el
   mismo contenido reenviado queda como duplicado) y, si se desea, se encola su lectura automática.
5. El correo se marca como leído y, opcionalmente, se mueve a una carpeta de procesados. Un mismo correo (Message-ID)
   nunca se procesa dos veces aunque se vuelva a marcar como no leído.

Aprendizaje del remitente: cuando una factura llegada por correo se aprueba, la dirección del remitente queda asociada a
ese proveedor. La siguiente factura del mismo remitente que no traiga NIF legible (CIF solo en el logotipo, escaneados)
toma el proveedor y su NIF de esa asociación, y se avisa en observaciones para que se compruebe.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from . import db
from .config import DATA_DIR, EMPRESA_NIF

SCHEMA = """
CREATE TABLE IF NOT EXISTS buzon_mensajes (
    id INTEGER PRIMARY KEY, message_id TEXT UNIQUE NOT NULL, uid TEXT, carpeta TEXT,
    remitente TEXT, remitente_email TEXT, asunto TEXT, fecha TEXT, recibido_en TEXT,
    estado TEXT, n_adjuntos INTEGER DEFAULT 0, n_facturas INTEGER DEFAULT 0, intentos INTEGER DEFAULT 0,
    error TEXT, lote TEXT, trabajo_id INTEGER);
CREATE TABLE IF NOT EXISTS buzon_adjuntos (
    id INTEGER PRIMARY KEY, mensaje_id INTEGER NOT NULL REFERENCES buzon_mensajes(id) ON DELETE CASCADE,
    nombre TEXT, sha256 TEXT, ruta TEXT, clasificacion TEXT, puntuacion INTEGER, motivos TEXT,
    documento_id INTEGER, decidido_por TEXT, decidido_en TEXT);
CREATE INDEX IF NOT EXISTS ix_badj_msg ON buzon_adjuntos(mensaje_id);
CREATE TABLE IF NOT EXISTS buzon_remitentes (
    email TEXT PRIMARY KEY, proveedor_id INTEGER REFERENCES proveedores(id) ON DELETE CASCADE,
    veces INTEGER DEFAULT 1, ultima TEXT);
"""

ESTADOS = {"procesado": "Procesado", "sin_adjuntos": "Sin adjuntos", "sin_facturas": "Sin facturas", "ignorado": "Remitente ignorado",
           "error": "Error (se reintenta)"}
CLASES = {"factura": "Factura", "dudoso": "Dudoso (decidir)", "no_factura": "No es factura", "duplicado": "Ya estaba",
          "error": "Archivo no válido", "importado": "Importado a mano"}
MAX_MENSAJES_POR_VUELTA = 50
MAX_INTENTOS = 5


def init(con):
    con.executescript(SCHEMA)
    try:
        con.execute("ALTER TABLE documentos ADD COLUMN buzon_mensaje_id INTEGER")
    except Exception:
        pass
    con.commit()


def carpeta_apartados() -> Path:
    p = DATA_DIR / "buzon"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config(con) -> dict:
    g = lambda k, d="": db.get_setting(con, "buzon_" + k, d)  # noqa: E731
    return {"activo": g("activo", "0") == "1", "host": g("host"), "puerto": int(g("puerto", "993") or 993),
            "seguridad": g("seguridad", "ssl"), "usuario": g("usuario"), "auth": g("auth", "clave"),
            "tenant": g("tenant"), "client_id": g("client_id"),
            "carpeta": g("carpeta", "INBOX") or "INBOX", "carpeta_procesados": g("carpeta_procesados"),
            "minutos": int(g("minutos", "10") or 10), "dias_atras": int(g("dias_atras", "30") or 30),
            "solo_no_leidos": g("solo_no_leidos", "1") == "1", "leer_auto": g("leer_auto", "1") == "1",
            "modo_ia": g("modo_ia", "auto"), "obra_defecto": int(g("obra_defecto", "0") or 0) or None,
            "soporte": g("soporte", "0") == "1",
            "ignorar": [x.strip().lower() for x in g("ignorar").split(",") if x.strip()]}


# ============================================================================ clasificación de adjuntos
def _plano(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[ \t]+", " ", t)


_RE_NIF = re.compile(r"\b(?:ES)?([ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]|\d{8}[A-Z]|[XYZ]\d{7}[A-Z])\b")


def clasificar_texto(texto: str, nombre: str = "", asunto: str = "") -> tuple[str, int, list[str]]:
    """Clasifica un documento por su contenido. Devuelve (clase, puntuación, motivos).
    clase: 'factura' | 'dudoso' | 'no_factura' (con subtipo en el primer motivo cuando es otro documento)."""
    from .fiscal import validate_nif, normalize_nif
    t = _plano(texto)
    n = _plano(nombre) + " " + _plano(asunto)
    pts, motivos = 0, []

    def suma(v, m):
        nonlocal pts
        pts += v
        motivos.append(f"{'+' if v > 0 else ''}{v} {m}")

    es_fact = re.search(r"\bfactura\b|\binvoice\b|\bfactura rectificativa\b|\bfra\.? ?n|\bn\.? ?de factura|\bfactura n|\bfactura simplificada", t)
    if es_fact:
        suma(4, "dice «factura»")
    if re.search(r"\babono\b|\brectificativa\b|\bnota de credito\b|\bcredit note\b", t):
        suma(2, "abono / rectificativa")
    if re.search(r"base imponible|base imp\.?|\bbase\b.{0,20}\d|taxable|net amount|base imposable", t):
        suma(2, "base imponible")
    if re.search(r"\bi\.?v\.?a\.?\b|\bigic\b|\bvat\b|inversion del sujeto pasivo|isp\b", t):
        suma(1, "IVA / ISP")
    if re.search(r"total (factura|a pagar|importe)|importe total|total a abonar|liquido|total amount|total eur", t):
        suma(1, "total de factura")
    if re.search(r"vencimiento|forma de pago|\biban\b|transferencia|due date", t):
        suma(1, "condiciones de pago")
    nifs = {normalize_nif(m.group(1)) for m in _RE_NIF.finditer((texto or "").upper().replace("-", "").replace(".", ""))}
    ajenos = [x for x in nifs if validate_nif(x)[0] and x != EMPRESA_NIF]
    if ajenos:
        suma(2, "NIF de un tercero válido")
    if re.search(r"\bfact|\bfra\b|\bfra[.\- _]|invoice|\bfv[\d_\-]|\bfa\d|\bfactura", n):
        suma(1, "el nombre del archivo o el asunto hablan de factura")
    # ---- otros documentos
    otros = [
        (r"%or\b.*\ban\b.*\bac\b", "certificación a cliente (ORIGEN/ANTERIOR/ACTUAL)", -8),
        (r"\bpresupuesto\b|\boferta\b|\bcotizacion\b|\bquotation\b", "presupuesto / oferta", -4),
        (r"\bproforma\b|\bpro forma\b|\bpro-forma\b", "factura proforma (no es factura)", -6),
        (r"\balbaran\b|\bnota de entrega\b|\bdelivery note\b", "albarán", -3),
        (r"\bcertificacion\b|\brelacion valorada\b", "certificación / relación valorada", -3),
        (r"\bnomina\b|\brecibo de salarios\b", "nómina", -8),
        (r"\bpedido\b|\borden de compra\b|\bpurchase order\b", "pedido", -2),
        (r"\bcontrato\b", "contrato", -2),
        (r"\bplano\b|\bficha tecnica\b|\bcatalogo\b|\bdeclaracion de prestaciones\b", "plano / ficha técnica", -4),
        (r"\bcertificado\b.{0,40}(corriente|hacienda|seguridad social|aeat)", "certificado administrativo", -6),
    ]
    for rx, nombre_doc, v in otros:
        if re.search(rx, t, re.S):
            # un albarán o una certificación citados DENTRO de una factura no la convierten en otro documento
            if es_fact and v > -6:
                v = -1
            suma(v, nombre_doc)
    if not t.strip():
        motivos.append("sin texto (escaneado): se clasifica por nombre y asunto")
    if pts >= 6:
        return "factura", pts, motivos
    if pts >= 3:
        return "dudoso", pts, motivos
    return "no_factura", pts, motivos


def clasificar_adjunto(nombre: str, data: bytes, asunto: str = "", usar_ocr: bool = True) -> tuple[str, int, list[str]]:
    from .pdf_utils import read_text, has_text_layer
    try:
        texto, _ = read_text(data, max_pages=4)
    except Exception:
        texto = ""
    if not has_text_layer(texto) and usar_ocr:
        try:
            from .extractor_local import ocr_pdf
            texto = ocr_pdf(data, max_paginas=1)
        except Exception:
            texto = ""
    clase, pts, mot = clasificar_texto(texto, nombre, asunto)
    if not has_text_layer(texto) and not texto.strip() and clase == "no_factura" and re.search(r"fact|fra|invoice", _plano(nombre + " " + asunto)):
        clase = "dudoso"
    return clase, pts, mot


# ============================================================================ mensajes
def _texto_cabecera(v) -> str:
    return str(v or "").replace("\r", " ").replace("\n", " ").strip()


def adjuntos_de_mensaje(msg) -> list[tuple[str, bytes]]:
    """Adjuntos «de verdad» del correo: partes con nombre de archivo, o PDF sin nombre."""
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        ctype = (part.get_content_type() or "").lower()
        if not fn and ctype not in ("application/pdf", "message/rfc822", "application/zip"):
            continue
        try:
            data = part.get_payload(decode=True)
        except Exception:
            data = None
        if ctype == "message/rfc822" and data is None:
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                data = inner[0].as_bytes()
                fn = fn or "reenviado.eml"
        if not data:
            continue
        if not fn:
            fn = {"application/pdf": "adjunto.pdf", "message/rfc822": "reenviado.eml", "application/zip": "adjunto.zip"}[ctype]
        out.append((fn, data))
    return out


def procesar_mensaje(con, raw: bytes, uid: str = "", carpeta: str = "", cfg: dict | None = None, usuario: str = "buzón") -> dict:
    """Procesa un correo completo. Idempotente por Message-ID. Devuelve el resumen."""
    from . import ingesta, historial
    from .pdf_utils import iter_pdfs_from_upload, sha256
    cfg = cfg or config(con)
    msg = email.message_from_bytes(raw, policy=policy.default)
    mid = _texto_cabecera(msg.get("Message-ID")) or "sin-id-" + hashlib.sha256(raw).hexdigest()[:32]
    previo = db.one(con, "SELECT * FROM buzon_mensajes WHERE message_id=?", (mid,))
    if previo and previo["estado"] != "error":
        return {"mensaje_id": previo["id"], "estado": "ya_procesado", "facturas": 0, "documentos": []}
    nombre, correo_rem = parseaddr(_texto_cabecera(msg.get("From")))
    correo_rem = (correo_rem or "").lower()
    asunto = _texto_cabecera(msg.get("Subject"))
    try:
        fecha = parsedate_to_datetime(msg.get("Date")).isoformat(timespec="seconds") if msg.get("Date") else None
    except Exception:
        fecha = None
    if previo:
        mid_db = previo["id"]
        con.execute("DELETE FROM buzon_adjuntos WHERE mensaje_id=? AND documento_id IS NULL", (mid_db,))
    else:
        cur = con.execute("INSERT INTO buzon_mensajes (message_id, uid, carpeta, remitente, remitente_email, asunto, fecha, recibido_en, estado) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", (mid, uid, carpeta, nombre, correo_rem, asunto, fecha, db.now_iso(), "error"))
        mid_db = cur.lastrowid
    con.commit()
    dominio = correo_rem.split("@")[-1] if "@" in correo_rem else ""
    if correo_rem and any(x in (correo_rem, dominio, "@" + dominio) for x in cfg["ignorar"]):
        con.execute("UPDATE buzon_mensajes SET estado='ignorado', error=NULL WHERE id=?", (mid_db,))
        con.commit()
        return {"mensaje_id": mid_db, "estado": "ignorado", "facturas": 0, "documentos": []}

    lote = historial.nuevo_lote()
    nuevos, n_adj, n_fact = [], 0, 0
    for fn, data in adjuntos_de_mensaje(msg):
        for nombre_pdf, pdf in iter_pdfs_from_upload(fn, data):
            n_adj += 1
            h = sha256(pdf)
            err = ingesta.comprobar_archivo(nombre_pdf, pdf)
            if err:
                con.execute("INSERT INTO buzon_adjuntos (mensaje_id, nombre, sha256, clasificacion, puntuacion, motivos) VALUES (?,?,?,?,?,?)",
                            (mid_db, nombre_pdf, h, "error", None, json.dumps([err], ensure_ascii=False)))
                continue
            existente = db.one(con, "SELECT id FROM documentos WHERE file_hash=?", (h,))
            if existente:
                con.execute("INSERT INTO buzon_adjuntos (mensaje_id, nombre, sha256, clasificacion, motivos, documento_id) VALUES (?,?,?,?,?,?)",
                            (mid_db, nombre_pdf, h, "duplicado", json.dumps(["el mismo PDF ya estaba registrado"], ensure_ascii=False),
                             existente["id"]))
                continue
            clase, pts, mot = clasificar_adjunto(nombre_pdf, pdf, asunto)
            soporte = cfg.get("soporte") and clase == "no_factura" and any(("albarán" in m or "certificación / relación" in m) for m in mot) \
                and not any("certificación a cliente" in m for m in mot)
            if clase == "factura" or soporte:
                did, es_nuevo = ingesta.registrar_pdf(con, nombre_pdf, pdf, usuario, obra_id=cfg.get("obra_defecto"), lote=lote)
                nota = f"Recibida por correo de {nombre or correo_rem} <{correo_rem}> el {fecha or db.now_iso()}: «{asunto[:120]}»"
                con.execute("UPDATE documentos SET buzon_mensaje_id=?, notas=TRIM(COALESCE(notas,'') || ' ' || ?) WHERE id=?",
                            (mid_db, nota, did))
                if soporte:
                    con.execute("UPDATE documentos SET tipo_documento=? WHERE id=?",
                                ("albaran" if any("albarán" in m for m in mot) else "certificacion", did))
                con.execute("INSERT INTO buzon_adjuntos (mensaje_id, nombre, sha256, clasificacion, puntuacion, motivos, documento_id) VALUES (?,?,?,?,?,?,?)",
                            (mid_db, nombre_pdf, h, clase if not soporte else "no_factura", pts, json.dumps(mot, ensure_ascii=False), did))
                if es_nuevo:
                    nuevos.append(did)
                    n_fact += 1
            else:
                ruta = carpeta_apartados() / f"{h}.pdf"
                if not ruta.exists():
                    ruta.write_bytes(pdf)
                con.execute("INSERT INTO buzon_adjuntos (mensaje_id, nombre, sha256, ruta, clasificacion, puntuacion, motivos) VALUES (?,?,?,?,?,?,?)",
                            (mid_db, nombre_pdf, h, str(ruta), clase, pts, json.dumps(mot, ensure_ascii=False)))
    estado = "procesado" if n_fact or nuevos else ("sin_adjuntos" if n_adj == 0 else "sin_facturas")
    tid = None
    if nuevos and cfg.get("leer_auto"):
        from . import trabajos
        leer = [d for d in nuevos if db.one(con, "SELECT estado FROM documentos WHERE id=?", (d,))["estado"] == "sin_procesar"]
        if leer:
            tid = trabajos.crear_lectura(con, leer, cfg.get("obra_defecto"), usuario, cfg.get("modo_ia") or "auto")
    con.execute("UPDATE buzon_mensajes SET estado=?, n_adjuntos=?, n_facturas=?, error=NULL, lote=?, trabajo_id=?, uid=?, carpeta=? WHERE id=?",
                (estado, n_adj, n_fact, lote, tid, uid, carpeta, mid_db))
    db.audit(con, usuario, "buzon_correo", "buzon", mid_db, {"de": correo_rem, "asunto": asunto[:120], "adjuntos": n_adj, "facturas": n_fact})
    con.commit()
    return {"mensaje_id": mid_db, "estado": estado, "facturas": n_fact, "documentos": nuevos, "trabajo": tid}


def importar_apartado(con, adj_id: int, usuario: str, obra_id: int | None = None, leer: bool = True) -> int:
    """El clasificador se equivocó (o era dudoso): el adjunto entra en el circuito como factura."""
    from . import ingesta, trabajos
    a = db.one(con, "SELECT a.*, m.remitente, m.remitente_email, m.asunto, m.fecha FROM buzon_adjuntos a JOIN buzon_mensajes m ON m.id=a.mensaje_id WHERE a.id=?",
               (adj_id,))
    if not a or not a["ruta"] or not Path(a["ruta"]).exists():
        raise ValueError("No se encuentra el archivo apartado.")
    did, nuevo = ingesta.registrar_pdf(con, a["nombre"], Path(a["ruta"]).read_bytes(), usuario, obra_id=obra_id)
    con.execute("UPDATE documentos SET buzon_mensaje_id=?, notas=TRIM(COALESCE(notas,'') || ' ' || ?) WHERE id=?",
                (a["mensaje_id"], f"Recibida por correo de {a['remitente'] or a['remitente_email']} el {a['fecha']}: «{(a['asunto'] or '')[:120]}» "
                                  f"(importada a mano por {usuario})", did))
    con.execute("UPDATE buzon_adjuntos SET clasificacion='importado', documento_id=?, decidido_por=?, decidido_en=? WHERE id=?",
                (did, usuario, db.now_iso(), adj_id))
    db.audit(con, usuario, "buzon_importar_apartado", "documento", did, {"adjunto": adj_id})
    con.commit()
    if nuevo and leer:
        trabajos.crear_lectura(con, [did], obra_id, usuario)
    return did


def descartar_apartado(con, adj_id: int, usuario: str) -> None:
    con.execute("UPDATE buzon_adjuntos SET clasificacion='no_factura', decidido_por=?, decidido_en=? WHERE id=?", (usuario, db.now_iso(), adj_id))
    db.audit(con, usuario, "buzon_descartar", "buzon_adjunto", adj_id, None)
    con.commit()


# ============================================================================ aprendizaje del remitente
def aprender_remitentes(con) -> int:
    """Asocia remitente → proveedor a partir de las facturas aprobadas que llegaron por correo."""
    n = 0
    for r in db.rows(con, """SELECT m.remitente_email AS email, d.proveedor_id, COUNT(*) veces, MAX(d.aprobado_en) ultima
                             FROM documentos d JOIN buzon_mensajes m ON m.id=d.buzon_mensaje_id
                             WHERE d.estado='aprobada' AND d.proveedor_id IS NOT NULL AND m.remitente_email LIKE '%@%'
                             GROUP BY m.remitente_email, d.proveedor_id ORDER BY veces"""):
        con.execute("""INSERT INTO buzon_remitentes (email, proveedor_id, veces, ultima) VALUES (?,?,?,?)
                       ON CONFLICT(email) DO UPDATE SET proveedor_id=excluded.proveedor_id, veces=excluded.veces, ultima=excluded.ultima""",
                    (r["email"], r["proveedor_id"], r["veces"], r["ultima"]))
        n += 1
    con.commit()
    return n


_GENERICOS = ("gmail.com", "hotmail.com", "outlook.com", "yahoo.es", "yahoo.com", "icloud.com", "live.com", "hotmail.es", "outlook.es")


def proveedor_por_remitente(con, correo_rem: str) -> dict | None:
    if not correo_rem:
        return None
    r = db.one(con, "SELECT p.* FROM buzon_remitentes b JOIN proveedores p ON p.id=b.proveedor_id WHERE b.email=?", (correo_rem.lower(),))
    if r:
        return r
    dom = correo_rem.lower().split("@")[-1]
    if dom and dom not in _GENERICOS:
        cands = db.rows(con, """SELECT DISTINCT p.* FROM buzon_remitentes b JOIN proveedores p ON p.id=b.proveedor_id
                                WHERE b.email LIKE ?""", ("%@" + dom,))
        if len(cands) == 1:
            return cands[0]
    return None


def completar_por_remitente(con, doc_id: int) -> bool:
    """Tras la lectura: si falta el NIF del emisor, se toma del proveedor habitual de ese remitente (y del IBAN conocido)."""
    d = db.one(con, """SELECT d.id, d.emisor_nif, d.proveedor_id, d.iban, m.remitente_email FROM documentos d
                       LEFT JOIN buzon_mensajes m ON m.id=d.buzon_mensaje_id WHERE d.id=?""", (doc_id,))
    if not d or (d["emisor_nif"] and d["proveedor_id"]):
        return False
    p = proveedor_por_remitente(con, d["remitente_email"]) if d["remitente_email"] else None
    via = "remitente del correo"
    if not p and d["iban"]:
        from .fiscal import normalize_iban
        cands = db.rows(con, """SELECT DISTINCT p.* FROM proveedor_ibans i JOIN proveedores p ON p.id=i.proveedor_id
                                WHERE i.iban=? AND COALESCE(i.verificado,0)=1""", (normalize_iban(d["iban"]),))
        if len(cands) == 1:
            p, via = cands[0], "IBAN verificado"
    if not p:
        return False
    con.execute("""UPDATE documentos SET proveedor_id=?, emisor_nif=COALESCE(emisor_nif, ?), emisor_nombre=COALESCE(emisor_nombre, ?),
                   observaciones_ia=TRIM(COALESCE(observaciones_ia,'') || ' ' || ?) WHERE id=?""",
                (p["id"], p["nif"], p["nombre"], f"Proveedor deducido por el {via} ({p['nombre']}): compruébelo.", doc_id))
    try:
        from .validation import validar_y_guardar
        validar_y_guardar(con, doc_id)
    except Exception:
        pass
    db.audit(con, "buzón", "proveedor_por_remitente", "documento", doc_id, {"proveedor": p["nombre"], "via": via})
    con.commit()
    return True


# ============================================================================ IMAP
def _conectar(con, cfg: dict):
    from . import credenciales
    if not cfg["host"] or not cfg["usuario"]:
        raise ValueError("Configure el servidor y el usuario del buzón en Configuración → Buzón de facturas.")
    if cfg["seguridad"] == "ssl":
        M = imaplib.IMAP4_SSL(cfg["host"], cfg["puerto"], timeout=60)
    else:
        M = imaplib.IMAP4(cfg["host"], cfg["puerto"], timeout=60)
        if cfg["seguridad"] == "starttls":
            M.starttls()
    if cfg["auth"] == "m365":
        tok = credenciales.token_m365(cfg["tenant"], cfg["client_id"], credenciales.leer(con, "buzon_client_secret"))
        M.authenticate("XOAUTH2", lambda _: credenciales.cadena_xoauth2(cfg["usuario"], tok).encode())
    else:
        clave = credenciales.leer(con, "buzon_clave")
        if not clave:
            raise ValueError("Falta la contraseña del buzón (Configuración → Buzón de facturas, o variable LLORCA_BUZON_CLAVE).")
        M.login(cfg["usuario"], clave)
    return M


_MESES_IMAP = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _q(carpeta: str) -> str:
    return '"' + carpeta.replace("\\", "\\\\").replace('"', '\\"') + '"'


def probar_conexion(con, cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or config(con)
    try:
        M = _conectar(con, cfg)
        typ, data = M.select(_q(cfg["carpeta"]), readonly=True)
        if typ != "OK":
            M.logout()
            return False, f"Conectado, pero no existe la carpeta «{cfg['carpeta']}»."
        typ, ids = M.search(None, "UNSEEN")
        n = len(ids[0].split()) if typ == "OK" and ids and ids[0] else 0
        if cfg["carpeta_procesados"]:
            t2, _ = M.select(_q(cfg["carpeta_procesados"]), readonly=True)
            if t2 != "OK":
                M.logout()
                return False, f"Conectado, pero no existe la carpeta de procesados «{cfg['carpeta_procesados']}»."
        M.logout()
        return True, f"Conexión correcta. {data[0].decode() if data and data[0] else '?'} correos en «{cfg['carpeta']}», {n} sin leer."
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def revisar(con, cfg: dict | None = None, usuario: str = "buzón") -> str:
    """Una vuelta completa por el buzón. Devuelve un resumen."""
    cfg = cfg or config(con)
    M = _conectar(con, cfg)
    try:
        typ, _ = M.select(_q(cfg["carpeta"]))
        if typ != "OK":
            raise ValueError(f"No existe la carpeta «{cfg['carpeta']}» en el buzón.")
        d = date.today() - timedelta(days=cfg["dias_atras"])
        desde = f"{d.day:02d}-{_MESES_IMAP[d.month - 1]}-{d.year}"      # formato IMAP, independiente del idioma del equipo
        criterio = f"(UNSEEN SINCE {desde})" if cfg["solo_no_leidos"] else f"(SINCE {desde})"
        typ, data = M.uid("SEARCH", None, criterio)
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        hechos = {"procesado": 0, "sin_facturas": 0, "sin_adjuntos": 0, "ignorado": 0, "ya_procesado": 0, "error": 0}
        facturas = 0
        mover = bool(cfg["carpeta_procesados"])
        puede_move = "MOVE" in {str(c).upper() for c in (M.capabilities or ())}
        for uid in uids[:MAX_MENSAJES_POR_VUELTA]:
            u = uid.decode()
            typ, partes = M.uid("FETCH", u, "(BODY.PEEK[])")
            raw = next((p[1] for p in partes or [] if isinstance(p, tuple) and len(p) > 1), None)
            if typ != "OK" or not raw:
                hechos["error"] += 1
                continue
            try:
                res = procesar_mensaje(con, raw, u, cfg["carpeta"], cfg, usuario)
            except Exception as e:  # noqa: BLE001 - un correo raro no para el resto
                con.rollback()
                msg = email.message_from_bytes(raw, policy=policy.default)
                mid = _texto_cabecera(msg.get("Message-ID")) or "sin-id-" + hashlib.sha256(raw).hexdigest()[:32]
                con.execute("""INSERT INTO buzon_mensajes (message_id, uid, carpeta, remitente_email, asunto, recibido_en, estado, intentos, error)
                               VALUES (?,?,?,?,?,?,'error',1,?) ON CONFLICT(message_id) DO UPDATE SET intentos=intentos+1, error=excluded.error""",
                            (mid, u, cfg["carpeta"], parseaddr(_texto_cabecera(msg.get("From")))[1].lower(),
                             _texto_cabecera(msg.get("Subject")), db.now_iso(), f"{type(e).__name__}: {e}"[:500]))
                con.commit()
                hechos["error"] += 1
                r = db.one(con, "SELECT intentos FROM buzon_mensajes WHERE message_id=?", (mid,))
                if r and r["intentos"] >= MAX_INTENTOS:
                    M.uid("STORE", u, "+FLAGS", "(\\Seen \\Flagged)")      # se deja marcado para revisarlo a mano
                continue
            hechos[res["estado"]] = hechos.get(res["estado"], 0) + 1
            facturas += res["facturas"]
            M.uid("STORE", u, "+FLAGS", "(\\Seen)")
            if mover and res["estado"] != "ya_procesado":
                if puede_move:
                    M.uid("MOVE", u, _q(cfg["carpeta_procesados"]))
                else:
                    if M.uid("COPY", u, _q(cfg["carpeta_procesados"]))[0] == "OK":
                        M.uid("STORE", u, "+FLAGS", "(\\Deleted)")
        if mover and not puede_move:
            M.expunge()
    finally:
        try:
            M.logout()
        except Exception:
            pass
    aprender_remitentes(con)
    partes = [f"{len(uids)} correo(s) nuevos", f"{facturas} factura(s) registradas"]
    partes += [f"{v} {k.replace('_', ' ')}" for k, v in hechos.items() if v and k != "procesado"]
    return " · ".join(partes)


def tareas():
    from .planificador import Tarea, cada_minutos
    return [Tarea("buzon", "Lectura del buzón de facturas", cada_minutos("buzon_minutos", 10, "buzon_activo"), revisar)]


def resumen(con, dias: int = 30) -> dict:
    desde = (datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds")
    r = db.one(con, """SELECT COUNT(*) n, SUM(estado='procesado') proc, SUM(estado='error') err, COALESCE(SUM(n_facturas),0) fact
                       FROM buzon_mensajes WHERE recibido_en>=?""", (desde,)) or {}
    dud = db.one(con, "SELECT COUNT(*) n FROM buzon_adjuntos WHERE clasificacion='dudoso'") or {}
    return {"mensajes": r.get("n") or 0, "procesados": r.get("proc") or 0, "errores": r.get("err") or 0, "facturas": r.get("fact") or 0,
            "dudosos": dud.get("n") or 0}
