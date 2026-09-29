"""
CONTROL DE VERSIONES DE PLANOS Y FICHAS TÉCNICAS.

Responde a dos preguntas que hoy cuestan: «¿cuál es la última versión?» y «¿qué aprobó la dirección facultativa (DF)?».

- Cada documento (plano, ficha técnica, memoria, detalle…) tiene código, título, disciplina y, opcionalmente, la
  subcontrata o el material al que afecta. Sus REVISIONES (A, B, C… o 00, 01…) guardan el archivo, la fecha y el estado:
  borrador → enviada a DF → aprobada / aprobada con comentarios / rechazada; y superada cuando se aprueba otra posterior.
- «Vigente» = la última revisión aprobada. Si hay una revisión posterior pendiente de la DF, se avisa: en obra se trabaja
  con la vigente, no con la última recibida.
- La aprobación registra quién de la DF aprobó, cuándo, con qué comentarios y el justificante (acta, correo, sello).
- El mismo archivo no entra dos veces (huella SHA-256), y el nombre del archivo propone código y revisión
  (p. ej. «ARQ-101_R03.pdf» → código ARQ-101, revisión 03).
- Distribución: a quién se entregó cada revisión (subcontratas, jefe de obra). Si se entregó una revisión que después
  queda superada, se avisa de que hay que retirar la copia antigua.
- Alertas: revisiones pendientes de la DF más de N días y entregas de revisiones superadas.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db
from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS planos_docs (
    id INTEGER PRIMARY KEY, obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    codigo TEXT NOT NULL, titulo TEXT NOT NULL, tipo TEXT DEFAULT 'plano', disciplina TEXT, afecta_a TEXT, notas TEXT,
    creado_por TEXT, creado_en TEXT, UNIQUE (obra_id, codigo));
CREATE TABLE IF NOT EXISTS planos_revisiones (
    id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES planos_docs(id) ON DELETE CASCADE,
    revision TEXT NOT NULL, fecha TEXT, archivo TEXT, nombre_archivo TEXT, sha256 TEXT, descripcion_cambios TEXT,
    estado TEXT NOT NULL DEFAULT 'borrador', enviado_df_en TEXT, enviado_df_por TEXT,
    resuelto_en TEXT, aprobado_por_df TEXT, comentarios_df TEXT, justificante TEXT, registrado_por TEXT,
    subido_por TEXT, subido_en TEXT, UNIQUE (doc_id, revision));
CREATE TABLE IF NOT EXISTS planos_distribucion (
    id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL REFERENCES planos_revisiones(id) ON DELETE CASCADE,
    destinatario TEXT NOT NULL, medio TEXT, fecha TEXT, usuario TEXT, retirada INTEGER DEFAULT 0, retirada_en TEXT);
"""
TIPOS = {"plano": "Plano", "ficha": "Ficha técnica", "detalle": "Detalle constructivo", "memoria": "Memoria / anejo", "otro": "Otro"}
DISCIPLINAS = ["Arquitectura", "Estructura", "Instalaciones eléctricas", "Fontanería y saneamiento", "Climatización", "PCI",
               "Telecomunicaciones", "Urbanización", "Carpintería", "Acabados", "Otra"]
ESTADOS = {"borrador": "Borrador (no enviada)", "enviada_df": "Enviada a la DF", "aprobada": "Aprobada",
           "aprobada_comentarios": "Aprobada con comentarios", "rechazada": "Rechazada", "superada": "Superada"}
APROBADAS = ("aprobada", "aprobada_comentarios")


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def carpeta(obra_id: int) -> Path:
    p = DATA_DIR / "planos" / str(obra_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


RE_REV = re.compile(r"^(?P<codigo>.+?)[\s_\-.]*(?:rev(?:isi[oó]n)?|r|ed|v)[\s_\-.]*(?P<rev>[0-9]{1,3}|[A-Z])$", re.I)


def proponer_desde_nombre(nombre: str) -> tuple[str, str | None]:
    """«ARQ-101_R03.pdf» → («ARQ-101», «03»); «EST 12 rev B» → («EST 12», «B»). Sin revisión → (nombre, None)."""
    base = Path(nombre).stem.strip()
    m = RE_REV.match(base)
    if m and len(m.group("codigo")) >= 2:
        return m.group("codigo").strip(" _-."), m.group("rev").upper()
    return base, None


def _orden_rev(r: str) -> tuple:
    r = str(r or "").strip().upper()
    return (0, int(r), "") if r.isdigit() else (1, 0, r)


def crear_documento(con, obra_id: int, codigo: str, titulo: str, tipo: str, disciplina: str, afecta_a: str, usuario: str) -> int:
    codigo, titulo = codigo.strip(), titulo.strip()
    if not codigo or not titulo:
        raise ValueError("Código y título son obligatorios.")
    if db.one(con, "SELECT id FROM planos_docs WHERE obra_id=? AND lower(codigo)=lower(?)", (obra_id, codigo)):
        raise ValueError(f"Ya existe el documento {codigo} en esta obra.")
    with db.tx(con):
        cur = con.execute("""INSERT INTO planos_docs (obra_id, codigo, titulo, tipo, disciplina, afecta_a, creado_por, creado_en)
                             VALUES (?,?,?,?,?,?,?,?)""", (obra_id, codigo, titulo, tipo, disciplina, afecta_a, usuario, db.now_iso()))
        db.audit(con, usuario, "plano_nuevo", "plano", cur.lastrowid, {"codigo": codigo})
    return cur.lastrowid


def subir_revision(con, doc_id: int, revision: str, nombre_archivo: str, data: bytes, usuario: str, fecha: str | None = None,
                   cambios: str = "") -> int:
    d = db.one(con, "SELECT * FROM planos_docs WHERE id=?", (doc_id,))
    revision = str(revision or "").strip().upper()
    if not revision:
        raise ValueError("Indique la revisión.")
    if not data:
        raise ValueError("Archivo vacío.")
    if db.one(con, "SELECT id FROM planos_revisiones WHERE doc_id=? AND revision=?", (doc_id, revision)):
        raise ValueError(f"La revisión {revision} de {d['codigo']} ya existe.")
    h = hashlib.sha256(data).hexdigest()
    ya = db.one(con, """SELECT r.revision, p.codigo FROM planos_revisiones r JOIN planos_docs p ON p.id=r.doc_id
                        WHERE r.sha256=? AND p.obra_id=?""", (h, d["obra_id"]))
    if ya:
        raise ValueError(f"Ese mismo archivo ya está registrado como {ya['codigo']} rev. {ya['revision']}.")
    ultima = max((r["revision"] for r in revisiones(con, doc_id)), key=_orden_rev, default=None)
    if ultima and _orden_rev(revision) < _orden_rev(ultima):
        raise ValueError(f"La revisión {revision} es anterior a la última registrada ({ultima}).")
    ext = Path(nombre_archivo).suffix.lower() or ".pdf"
    ruta = carpeta(d["obra_id"]) / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', d['codigo'])}_rev{revision}_{h[:8]}{ext}"
    ruta.write_bytes(data)
    with db.tx(con):
        cur = con.execute("""INSERT INTO planos_revisiones (doc_id, revision, fecha, archivo, nombre_archivo, sha256, descripcion_cambios, estado,
                             subido_por, subido_en) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (doc_id, revision, fecha or date.today().isoformat(), str(ruta), nombre_archivo, h, cambios, "borrador", usuario, db.now_iso()))
        db.audit(con, usuario, "plano_revision", "plano", doc_id, {"revision": revision, "archivo": nombre_archivo})
    return cur.lastrowid


def enviar_df(con, rev_id: int, usuario: str) -> None:
    r = db.one(con, "SELECT * FROM planos_revisiones WHERE id=?", (rev_id,))
    if r["estado"] != "borrador":
        raise ValueError("Solo se envía a la DF una revisión en borrador.")
    with db.tx(con):
        con.execute("UPDATE planos_revisiones SET estado='enviada_df', enviado_df_en=?, enviado_df_por=? WHERE id=?", (db.now_iso(), usuario, rev_id))
        db.audit(con, usuario, "plano_enviado_df", "plano", r["doc_id"], {"revision": r["revision"]})


def resolver_df(con, rev_id: int, estado: str, aprobado_por_df: str, comentarios: str, usuario: str, justificante: bytes | None = None,
                nombre_justificante: str = "", fecha: str | None = None) -> None:
    """Registra la respuesta de la DF. Al aprobar, las revisiones aprobadas anteriores pasan a «superadas»."""
    if estado not in ("aprobada", "aprobada_comentarios", "rechazada"):
        raise ValueError("Estado no válido.")
    r = db.one(con, "SELECT r.*, p.obra_id, p.codigo FROM planos_revisiones r JOIN planos_docs p ON p.id=r.doc_id WHERE r.id=?", (rev_id,))
    if r["estado"] not in ("enviada_df", "borrador"):
        raise ValueError("Esta revisión ya está resuelta.")
    if not aprobado_por_df.strip():
        raise ValueError("Indique quién de la dirección facultativa resuelve.")
    if estado == "aprobada_comentarios" and not comentarios.strip():
        raise ValueError("Anote los comentarios de la DF.")
    just = None
    if justificante:
        just = carpeta(r["obra_id"]) / f"DF_{re.sub(r'[^A-Za-z0-9_.-]', '_', r['codigo'])}_rev{r['revision']}{Path(nombre_justificante).suffix or '.pdf'}"
        just.write_bytes(justificante)
    with db.tx(con):
        con.execute("""UPDATE planos_revisiones SET estado=?, resuelto_en=?, aprobado_por_df=?, comentarios_df=?, justificante=COALESCE(?, justificante),
                       registrado_por=?, enviado_df_en=COALESCE(enviado_df_en, ?) WHERE id=?""",
                    (estado, fecha or db.now_iso(), aprobado_por_df.strip(), comentarios.strip(), str(just) if just else None, usuario, db.now_iso(), rev_id))
        if estado in APROBADAS:
            for o in revisiones(con, r["doc_id"]):
                if o["id"] != rev_id and o["estado"] in APROBADAS and _orden_rev(o["revision"]) < _orden_rev(r["revision"]):
                    con.execute("UPDATE planos_revisiones SET estado='superada' WHERE id=?", (o["id"],))
        db.audit(con, usuario, "plano_resolucion_df", "plano", r["doc_id"], {"revision": r["revision"], "estado": estado, "df": aprobado_por_df})


def revisiones(con, doc_id: int) -> list[dict]:
    return sorted(db.rows(con, "SELECT * FROM planos_revisiones WHERE doc_id=?", (doc_id,)), key=lambda r: _orden_rev(r["revision"]), reverse=True)


def vigente(con, doc_id: int) -> dict | None:
    return next((r for r in revisiones(con, doc_id) if r["estado"] in APROBADAS), None)


def listado(con, obra_id: int) -> list[dict]:
    out = []
    for d in db.rows(con, "SELECT * FROM planos_docs WHERE obra_id=? ORDER BY disciplina, codigo", (obra_id,)):
        revs = revisiones(con, d["id"])
        v = next((r for r in revs if r["estado"] in APROBADAS), None)
        ult = revs[0] if revs else None
        pend = [r for r in revs if r["estado"] == "enviada_df"]
        aviso = ""
        if not v and ult:
            aviso = "Sin ninguna revisión aprobada por la DF"
        elif v and ult and ult["id"] != v["id"] and ult["estado"] in ("enviada_df", "borrador"):
            aviso = f"Hay una revisión posterior ({ult['revision']}) sin aprobar: en obra, usar la {v['revision']}"
        elif v and v["estado"] == "aprobada_comentarios":
            aviso = "Vigente con comentarios de la DF"
        out.append({**d, "vigente": v["revision"] if v else None, "vigente_id": v["id"] if v else None,
                    "vigente_fecha": (v["resuelto_en"] or "")[:10] if v else None, "aprobado_por_df": v["aprobado_por_df"] if v else None,
                    "ultima": ult["revision"] if ult else None, "ultima_estado": ult["estado"] if ult else None,
                    "pendientes_df": len(pend), "revisiones": len(revs), "aviso": aviso})
    return out


def distribuir(con, rev_id: int, destinatarios: list[str], medio: str, usuario: str) -> int:
    n = 0
    with db.tx(con):
        for d in destinatarios:
            if d.strip():
                con.execute("INSERT INTO planos_distribucion (revision_id, destinatario, medio, fecha, usuario) VALUES (?,?,?,?,?)",
                            (rev_id, d.strip(), medio, date.today().isoformat(), usuario))
                n += 1
        db.audit(con, usuario, "plano_distribucion", "plano_revision", rev_id, {"destinatarios": destinatarios})
    return n


def entregas_a_retirar(con, obra_id: int | None = None) -> list[dict]:
    """Copias entregadas de revisiones ya superadas (o rechazadas) que nadie ha marcado como retiradas."""
    q = """SELECT x.*, r.revision, r.estado, p.codigo, p.titulo, p.obra_id FROM planos_distribucion x JOIN planos_revisiones r ON r.id=x.revision_id
           JOIN planos_docs p ON p.id=r.doc_id WHERE x.retirada=0 AND r.estado IN ('superada','rechazada')"""
    return db.rows(con, q + (" AND p.obra_id=?" if obra_id else ""), (obra_id,) if obra_id else ())


def marcar_retirada(con, dist_id: int, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE planos_distribucion SET retirada=1, retirada_en=? WHERE id=?", (db.now_iso(), dist_id))
        db.audit(con, usuario, "plano_copia_retirada", "plano_distribucion", dist_id, None)


def pendientes_df(con, obra_id: int | None = None, dias: int | None = None) -> list[dict]:
    dias = dias if dias is not None else int(db.get_setting(con, "planos_dias_df", "10") or 10)
    lim = (datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds")
    q = """SELECT r.*, p.codigo, p.titulo, p.obra_id FROM planos_revisiones r JOIN planos_docs p ON p.id=r.doc_id
           WHERE r.estado='enviada_df' AND r.enviado_df_en < ?"""
    p = [lim]
    if obra_id:
        q += " AND p.obra_id=?"; p.append(obra_id)
    return db.rows(con, q + " ORDER BY r.enviado_df_en", p)
