"""
Red de seguridad frente a errores de usuario: TODO se puede deshacer.

1. Versiones de cada documento: antes de cualquier cambio (edición de cabecera, líneas, IVA, relectura, cambio de
   estado) se guarda una foto completa (cabecera + líneas + IVA). Cualquier versión se puede restaurar.
2. Papelera de objetos: certificaciones, obras, estructuras de capítulos y fusiones de proveedores se guardan
   íntegras antes de eliminarse o modificarse, y se pueden restaurar tal cual (mismos identificadores).
3. Lotes: cada subida de archivos y cada lectura en lote llevan un identificador; se pueden deshacer enteras.
Todo queda además en la auditoría.
"""
from __future__ import annotations

import json
import uuid

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_versiones (
    id INTEGER PRIMARY KEY,
    documento_id INTEGER NOT NULL,
    ts TEXT, usuario TEXT, motivo TEXT,
    foto TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dv_doc ON doc_versiones(documento_id);
CREATE TABLE IF NOT EXISTS papelera_objetos (
    id INTEGER PRIMARY KEY,
    tipo TEXT NOT NULL,            -- certificacion | obra | estructura | fusion_proveedores
    ref TEXT, descripcion TEXT,
    datos TEXT NOT NULL,
    usuario TEXT, ts TEXT,
    restaurado INTEGER DEFAULT 0, restaurado_por TEXT, restaurado_en TEXT
);
"""

TABLAS_OBRA = ["partidas", "ofertas", "costes_manuales", "aud_parametros", "aud_categorias", "aud_proveedor_cat",
               "aud_criterios", "compras_sis", "rrhh_sis", "aud_historico"]
CAMPOS_NO_VERSIONADOS = {"id", "file_hash", "file_path", "filename", "paginas", "tiene_texto", "texto", "texto_hash",
                         "creado_en", "extraccion_json"}


def init(con):
    con.executescript(SCHEMA)
    try:
        con.execute("ALTER TABLE documentos ADD COLUMN lote_subida TEXT")
    except Exception:
        pass
    con.commit()


# ============================================================================ 1. versiones de documento
def foto(con, doc_id: int) -> dict:
    d = db.one(con, "SELECT * FROM documentos WHERE id=?", (doc_id,)) or {}
    return {"documento": {k: v for k, v in d.items() if k not in CAMPOS_NO_VERSIONADOS},
            "lineas": db.rows(con, "SELECT * FROM lineas WHERE documento_id=? ORDER BY orden, id", (doc_id,)),
            "impuestos": db.rows(con, "SELECT * FROM impuestos WHERE documento_id=? ORDER BY id", (doc_id,))}


def guardar_version(con, doc_id: int, usuario: str, motivo: str) -> None:
    f = foto(con, doc_id)
    if not f["documento"]:
        return
    ultima = db.one(con, "SELECT foto FROM doc_versiones WHERE documento_id=? ORDER BY id DESC LIMIT 1", (doc_id,))
    nueva = json.dumps(f, ensure_ascii=False, default=str, sort_keys=True)
    if ultima and ultima["foto"] == nueva:
        return                                   # sin cambios desde la última foto
    con.execute("INSERT INTO doc_versiones (documento_id, ts, usuario, motivo, foto) VALUES (?,?,?,?,?)",
                (doc_id, db.now_iso(), usuario, motivo, nueva))


def versiones(con, doc_id: int) -> list[dict]:
    return db.rows(con, "SELECT id, ts, usuario, motivo, foto FROM doc_versiones WHERE documento_id=? ORDER BY id DESC", (doc_id,))


def restaurar_version(con, version_id: int, usuario: str) -> int:
    v = db.one(con, "SELECT * FROM doc_versiones WHERE id=?", (version_id,))
    if not v:
        raise ValueError("Versión no encontrada.")
    doc_id = v["documento_id"]
    f = json.loads(v["foto"])
    guardar_version(con, doc_id, usuario, "antes de restaurar una versión anterior")
    cols = [c for c in f["documento"] if c not in CAMPOS_NO_VERSIONADOS]
    existentes = {r["name"] for r in db.rows(con, "PRAGMA table_info(documentos)")}
    cols = [c for c in cols if c in existentes]
    with db.tx(con):
        con.execute(f"UPDATE documentos SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                    [f["documento"][c] for c in cols] + [doc_id])
        con.execute("DELETE FROM lineas WHERE documento_id=?", (doc_id,))
        cl = {r["name"] for r in db.rows(con, "PRAGMA table_info(lineas)")} - {"id"}
        for l in f["lineas"]:
            ks = [k for k in l if k in cl]
            con.execute(f"INSERT INTO lineas ({', '.join(ks)}) VALUES ({','.join('?' * len(ks))})", [l[k] for k in ks])
        con.execute("DELETE FROM impuestos WHERE documento_id=?", (doc_id,))
        ci = {r["name"] for r in db.rows(con, "PRAGMA table_info(impuestos)")} - {"id"}
        for t in f["impuestos"]:
            ks = [k for k in t if k in ci]
            con.execute(f"INSERT INTO impuestos ({', '.join(ks)}) VALUES ({','.join('?' * len(ks))})", [t[k] for k in ks])
        db.audit(con, usuario, "restaurar_version", "documento", doc_id, {"version": version_id, "de": v["ts"]})
    from .validation import validar_y_guardar
    validar_y_guardar(con, doc_id)
    return doc_id


# ============================================================================ 2. papelera de objetos
def _a_papelera(con, tipo: str, ref: str, descripcion: str, datos: dict, usuario: str) -> int:
    cur = con.execute("INSERT INTO papelera_objetos (tipo, ref, descripcion, datos, usuario, ts) VALUES (?,?,?,?,?,?)",
                      (tipo, str(ref), descripcion, json.dumps(datos, ensure_ascii=False, default=str), usuario, db.now_iso()))
    return cur.lastrowid


def _insertar(con, tabla: str, filas: list[dict]) -> None:
    cols = {r["name"] for r in db.rows(con, f"PRAGMA table_info({tabla})")}
    for f in filas:
        ks = [k for k in f if k in cols]
        con.execute(f"INSERT OR REPLACE INTO {tabla} ({', '.join(ks)}) VALUES ({','.join('?' * len(ks))})", [f[k] for k in ks])


def eliminar_certificacion(con, cert_id: int, usuario: str) -> int:
    c = db.one(con, "SELECT * FROM certificaciones WHERE id=?", (cert_id,))
    if not c:
        raise ValueError("Certificación no encontrada.")
    datos = {"certificacion": c,
             "capitulos": db.rows(con, "SELECT * FROM cert_capitulos WHERE cert_id=?", (cert_id,)),
             "lineas": db.rows(con, "SELECT * FROM cert_lineas WHERE cert_id=?", (cert_id,))}
    obra = db.one(con, "SELECT codigo FROM obras WHERE id=?", (c["obra_id"],))
    with db.tx(con):
        pid = _a_papelera(con, "certificacion", cert_id, f"Certificación nº {c['numero']} ({c['fecha']}) · obra {obra and obra['codigo']}",
                          datos, usuario)
        con.execute("DELETE FROM certificaciones WHERE id=?", (cert_id,))
        db.audit(con, usuario, "eliminar_certificacion", "certificacion", cert_id, {"papelera": pid, "numero": c["numero"]})
    return pid


def eliminar_obra(con, obra_id: int, usuario: str) -> int:
    """Solo obras sin documentos ni certificaciones (para no dejar facturas huérfanas)."""
    o = db.one(con, "SELECT * FROM obras WHERE id=?", (obra_id,))
    if not o:
        raise ValueError("Obra no encontrada.")
    n_docs = db.one(con, "SELECT COUNT(*) n FROM documentos WHERE obra_id=? AND estado<>'eliminado'", (obra_id,))["n"]
    n_cert = db.one(con, "SELECT COUNT(*) n FROM certificaciones WHERE obra_id=?", (obra_id,))["n"]
    if n_docs or n_cert:
        raise ValueError(f"La obra tiene {n_docs} documento(s) y {n_cert} certificación(es). Reasígnelos o elimínelos antes; "
                         "si solo quiere ocultarla, desactívela.")
    datos = {"obra": o}
    for t in TABLAS_OBRA:
        try:
            datos[t] = db.rows(con, f"SELECT * FROM {t} WHERE obra_id=?", (obra_id,))
        except Exception:
            datos[t] = []
    with db.tx(con):
        pid = _a_papelera(con, "obra", obra_id, f"Obra {o['codigo']} · {o['nombre']}", datos, usuario)
        con.execute("UPDATE documentos SET obra_id=NULL WHERE obra_id=?", (obra_id,))
        con.execute("DELETE FROM obras WHERE id=?", (obra_id,))
        db.audit(con, usuario, "eliminar_obra", "obra", obra_id, {"papelera": pid})
    return pid


def foto_estructura(con, obra_id: int, usuario: str, motivo: str) -> int:
    """Antes de cambiar la estructura de partidas de una obra: guarda partidas e imputaciones para poder volver."""
    datos = {"obra_id": obra_id,
             "partidas": db.rows(con, "SELECT * FROM partidas WHERE obra_id=?", (obra_id,)),
             "lineas": db.rows(con, """SELECT l.id, l.partida_id, l.partida_origen, l.partida_confianza FROM lineas l
                                       JOIN documentos d ON d.id=l.documento_id WHERE d.obra_id=?""", (obra_id,)),
             "ofertas": db.rows(con, "SELECT id, partida_id FROM ofertas WHERE obra_id=?", (obra_id,)),
             "costes_manuales": db.rows(con, "SELECT id, partida_id FROM costes_manuales WHERE obra_id=?", (obra_id,))}
    o = db.one(con, "SELECT codigo FROM obras WHERE id=?", (obra_id,))
    return _a_papelera(con, "estructura", obra_id, f"Estructura de partidas de la obra {o and o['codigo']} ({motivo})", datos, usuario)


def fusionar_proveedores(con, destino_id: int, origen_id: int, usuario: str) -> int:
    """Une dos fichas del mismo proveedor (p. ej. uno con NIF y otro sin él). Reversible."""
    if destino_id == origen_id:
        raise ValueError("Elija dos proveedores distintos.")
    o = db.one(con, "SELECT * FROM proveedores WHERE id=?", (origen_id,))
    dst = db.one(con, "SELECT * FROM proveedores WHERE id=?", (destino_id,))
    if not o or not dst:
        raise ValueError("Proveedor no encontrado.")
    datos = {"origen": o, "destino_id": destino_id,
             "documentos": [r["id"] for r in db.rows(con, "SELECT id FROM documentos WHERE proveedor_id=?", (origen_id,))],
             "ofertas": [r["id"] for r in db.rows(con, "SELECT id FROM ofertas WHERE proveedor_id=?", (origen_id,))],
             "ibans": db.rows(con, "SELECT * FROM proveedor_ibans WHERE proveedor_id=?", (origen_id,))}
    with db.tx(con):
        pid = _a_papelera(con, "fusion_proveedores", origen_id, f"Fusión: «{o['nombre']}» dentro de «{dst['nombre']}»", datos, usuario)
        con.execute("UPDATE documentos SET proveedor_id=? WHERE proveedor_id=?", (destino_id, origen_id))
        con.execute("UPDATE ofertas SET proveedor_id=? WHERE proveedor_id=?", (destino_id, origen_id))
        for ib in datos["ibans"]:
            con.execute("INSERT OR IGNORE INTO proveedor_ibans (proveedor_id, iban, primera_vez, ultima_vez, veces, verificado, verificado_por) "
                        "VALUES (?,?,?,?,?,?,?)", (destino_id, ib["iban"], ib["primera_vez"], ib["ultima_vez"], ib["veces"],
                                                   ib["verificado"], ib["verificado_por"]))
        con.execute("DELETE FROM proveedor_ibans WHERE proveedor_id=?", (origen_id,))
        con.execute("DELETE FROM proveedores WHERE id=?", (origen_id,))
        db.audit(con, usuario, "fusionar_proveedores", "proveedor", destino_id, {"origen": origen_id, "papelera": pid})
    return pid


def objetos(con, solo_pendientes: bool = True) -> list[dict]:
    q = "SELECT id, tipo, ref, descripcion, usuario, ts, restaurado, restaurado_por, restaurado_en FROM papelera_objetos"
    return db.rows(con, q + (" WHERE restaurado=0" if solo_pendientes else "") + " ORDER BY id DESC")


def restaurar_objeto(con, papelera_id: int, usuario: str) -> str:
    p = db.one(con, "SELECT * FROM papelera_objetos WHERE id=?", (papelera_id,))
    if not p or p["restaurado"]:
        raise ValueError("Ya restaurado o inexistente.")
    d = json.loads(p["datos"])
    with db.tx(con):
        if p["tipo"] == "certificacion":
            c = d["certificacion"]
            if db.one(con, "SELECT id FROM certificaciones WHERE obra_id=? AND numero=?", (c["obra_id"], c["numero"])):
                raise ValueError(f"Ya existe otra certificación nº {c['numero']} en esa obra. Elimínela antes de restaurar esta.")
            if not db.one(con, "SELECT id FROM obras WHERE id=?", (c["obra_id"],)):
                raise ValueError("La obra de esta certificación ya no existe. Restaure antes la obra.")
            _insertar(con, "certificaciones", [c])
            _insertar(con, "cert_capitulos", d["capitulos"])
            _insertar(con, "cert_lineas", d["lineas"])
        elif p["tipo"] == "obra":
            if db.one(con, "SELECT id FROM obras WHERE codigo=?", (d["obra"]["codigo"],)):
                raise ValueError(f"Ya existe otra obra con el código {d['obra']['codigo']}.")
            _insertar(con, "obras", [d["obra"]])
            for t in TABLAS_OBRA:
                if d.get(t):
                    _insertar(con, t, d[t])
        elif p["tipo"] == "estructura":
            oid = d["obra_id"]
            actuales = [r["id"] for r in db.rows(con, "SELECT id FROM partidas WHERE obra_id=?", (oid,))]
            _insertar(con, "partidas", d["partidas"])
            for l in d["lineas"]:
                con.execute("UPDATE lineas SET partida_id=?, partida_origen=?, partida_confianza=? WHERE id=?",
                            (l["partida_id"], l["partida_origen"], l["partida_confianza"], l["id"]))
            for tabla in ("ofertas", "costes_manuales"):
                for r in d[tabla]:
                    con.execute(f"UPDATE {tabla} SET partida_id=? WHERE id=?", (r["partida_id"], r["id"]))
            viejas = {x["id"] for x in d["partidas"]}
            for pid_ in actuales:
                if pid_ not in viejas:
                    con.execute("UPDATE lineas SET partida_id=NULL WHERE partida_id=?", (pid_,))
                    con.execute("DELETE FROM partidas WHERE id=?", (pid_,))
        elif p["tipo"] == "fusion_proveedores":
            o = d["origen"]
            _insertar(con, "proveedores", [o])
            if d["documentos"]:
                con.execute(f"UPDATE documentos SET proveedor_id=? WHERE id IN ({','.join('?' * len(d['documentos']))})",
                            [o["id"]] + d["documentos"])
            if d["ofertas"]:
                con.execute(f"UPDATE ofertas SET proveedor_id=? WHERE id IN ({','.join('?' * len(d['ofertas']))})",
                            [o["id"]] + d["ofertas"])
            for ib in d["ibans"]:
                con.execute("DELETE FROM proveedor_ibans WHERE proveedor_id=? AND iban=? AND veces<=?",
                            (d["destino_id"], ib["iban"], ib["veces"]))
            _insertar(con, "proveedor_ibans", d["ibans"])
        con.execute("UPDATE papelera_objetos SET restaurado=1, restaurado_por=?, restaurado_en=? WHERE id=?",
                    (usuario, db.now_iso(), papelera_id))
        db.audit(con, usuario, f"restaurar_{p['tipo']}", "papelera", papelera_id, {"ref": p["ref"]})
    return p["descripcion"]


# ============================================================================ 3. lotes (subida / lectura)
def nuevo_lote() -> str:
    return uuid.uuid4().hex[:12]


def lotes_subida(con, n: int = 15) -> list[dict]:
    return db.rows(con, """SELECT lote_subida AS lote, MIN(creado_en) AS fecha, COUNT(*) AS documentos,
                                  SUM(estado='eliminado') AS en_papelera
                           FROM documentos WHERE lote_subida IS NOT NULL GROUP BY lote_subida ORDER BY fecha DESC LIMIT ?""", (n,))


def deshacer_subida(con, lote: str, usuario: str) -> int:
    ids = [r["id"] for r in db.rows(con, "SELECT id FROM documentos WHERE lote_subida=? AND estado<>'eliminado'", (lote,))]
    with db.tx(con):
        for i in ids:
            guardar_version(con, i, usuario, "antes de deshacer la subida")
            con.execute("UPDATE documentos SET estado_previo=estado, estado='eliminado' WHERE id=?", (i,))
        db.audit(con, usuario, "deshacer_subida", "lote", None, {"lote": lote, "documentos": len(ids)})
    return len(ids)


def deshacer_lectura(con, trabajo_id: int, usuario: str) -> int:
    """Devuelve cada documento del lote de lectura a como estaba antes de leerse (su versión previa)."""
    items = db.rows(con, "SELECT documento_id, iniciado_en FROM trabajo_items WHERE trabajo_id=? AND estado IN ('ok','revisar','duplicado')",
                    (trabajo_id,))
    n = 0
    for it in items:
        v = db.one(con, "SELECT id FROM doc_versiones WHERE documento_id=? AND motivo='antes de leer' AND ts<=COALESCE(?, ts) "
                        "ORDER BY id DESC LIMIT 1", (it["documento_id"], None))
        if v:
            restaurar_version(con, v["id"], usuario)
            n += 1
    with db.tx(con):
        con.execute("UPDATE trabajos SET mensaje='deshecho' WHERE id=?", (trabajo_id,))
        db.audit(con, usuario, "deshacer_lectura", "trabajo", trabajo_id, {"documentos": n})
    return n
