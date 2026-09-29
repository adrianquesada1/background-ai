"""
Trabajos en segundo plano (lectura de lotes de facturas).

La lectura ya no depende de la página abierta: se encola en la base de datos y un proceso interno la ejecuta
aunque el usuario cambie de pantalla, cierre la pestaña o se reinicie la aplicación (continúa donde se quedó).
Cada documento guarda su resultado; el progreso se consulta desde cualquier página.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS trabajos (
    id INTEGER PRIMARY KEY,
    tipo TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'pendiente',
    creado_por TEXT, creado_en TEXT, iniciado_en TEXT, terminado_en TEXT,
    total INTEGER DEFAULT 0, hechos INTEGER DEFAULT 0, errores INTEGER DEFAULT 0,
    obra_lote INTEGER, cancelar INTEGER DEFAULT 0, mensaje TEXT
);
CREATE TABLE IF NOT EXISTS trabajo_items (
    id INTEGER PRIMARY KEY,
    trabajo_id INTEGER NOT NULL REFERENCES trabajos(id) ON DELETE CASCADE,
    documento_id INTEGER NOT NULL, orden INTEGER,
    estado TEXT DEFAULT 'pendiente', resultado TEXT, segundos REAL, lectura TEXT, error TEXT, terminado_en TEXT
);
CREATE INDEX IF NOT EXISTS ix_ti_trab ON trabajo_items(trabajo_id);
"""


def init(con):
    con.executescript(SCHEMA)
    for tabla, col in (("trabajos", "modo_ia TEXT"), ("trabajo_items", "iniciado_en TEXT")):
        try:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {col}")
        except Exception:
            pass
    con.commit()


def crear_lectura(con, doc_ids: list[int], obra_lote: int | None, usuario: str, modo_ia: str = "auto") -> int:
    with db.tx(con):
        cur = con.execute("INSERT INTO trabajos (tipo, estado, creado_por, creado_en, total, obra_lote, modo_ia) VALUES (?,?,?,?,?,?,?)",
                          ("lectura", "pendiente", usuario, db.now_iso(), len(doc_ids), obra_lote, modo_ia))
        tid = cur.lastrowid
        for i, d in enumerate(doc_ids):
            con.execute("INSERT INTO trabajo_items (trabajo_id, documento_id, orden) VALUES (?,?,?)", (tid, d, i))
        db.audit(con, usuario, "encolar_lectura", "trabajo", tid, {"documentos": len(doc_ids)})
    despertar()
    return tid


def vaciar_cola(con, usuario: str) -> tuple[int, int]:
    """Quita de la cola todo lo que aún no se ha empezado a leer. El documento que se esté leyendo en ese momento termina
    (no se corta a medias) y el lote se da por cancelado. Los documentos quedan «sin leer», listos para otra lectura."""
    activos = [r["id"] for r in db.rows(con, "SELECT id FROM trabajos WHERE estado IN ('pendiente','en_curso')")]
    if not activos:
        return 0, 0
    marcas = ",".join("?" * len(activos))
    with db.tx(con):
        n_items = con.execute(f"UPDATE trabajo_items SET estado='cancelado', terminado_en=? WHERE trabajo_id IN ({marcas}) "
                              "AND estado='pendiente' AND iniciado_en IS NULL", [db.now_iso()] + activos).rowcount
        con.execute(f"UPDATE trabajos SET cancelar=1 WHERE id IN ({marcas})", activos)
        con.execute(f"UPDATE trabajos SET estado='cancelado', terminado_en=? WHERE id IN ({marcas}) AND estado='pendiente'",
                    [db.now_iso()] + activos)
        db.audit(con, usuario, "vaciar_cola", "trabajo", None, {"lotes": activos, "documentos_quitados": n_items})
    return len(activos), n_items


def cancelar(con, tid: int, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE trabajos SET cancelar=1 WHERE id=?", (tid,))
        db.audit(con, usuario, "cancelar_lectura", "trabajo", tid, None)


def activo(con) -> dict | None:
    return db.one(con, "SELECT * FROM trabajos WHERE estado IN ('pendiente','en_curso') ORDER BY id LIMIT 1")


def recientes(con, n: int = 5) -> list[dict]:
    return db.rows(con, "SELECT * FROM trabajos ORDER BY id DESC LIMIT ?", (n,))


def items(con, tid: int) -> list[dict]:
    return db.rows(con, """SELECT i.*, d.filename FROM trabajo_items i LEFT JOIN documentos d ON d.id=i.documento_id
                           WHERE trabajo_id=? ORDER BY CASE WHEN i.estado='pendiente' THEN 1 ELSE 0 END, i.terminado_en DESC, i.orden""",
                   (tid,))


def estadisticas(con, t: dict) -> dict:
    """Progreso real: %, tiempo medio por documento, estimación de lo que falta y documento en lectura."""
    hechos = db.rows(con, "SELECT segundos FROM trabajo_items WHERE trabajo_id=? AND estado<>'pendiente' AND segundos IS NOT NULL",
                     (t["id"],))
    media = (sum(h["segundos"] for h in hechos) / len(hechos)) if hechos else None
    actual = db.one(con, """SELECT i.iniciado_en, d.filename FROM trabajo_items i LEFT JOIN documentos d ON d.id=i.documento_id
                            WHERE i.trabajo_id=? AND i.estado='pendiente' AND i.iniciado_en IS NOT NULL ORDER BY i.orden LIMIT 1""",
                    (t["id"],)) if t["estado"] == "en_curso" else None
    lleva = None
    if actual and actual["iniciado_en"]:
        from datetime import datetime
        lleva = (datetime.now() - datetime.fromisoformat(actual["iniciado_en"])).total_seconds()
    restantes = max(0, (t["total"] or 0) - (t["hechos"] or 0))
    eta = media * restantes if media is not None else None
    return {"pct": round(100 * (t["hechos"] or 0) / max(1, t["total"] or 1)), "media": media, "eta": eta,
            "actual": actual["filename"] if actual else None, "lleva": lleva}


def en_cola(con) -> set[int]:
    return {r["documento_id"] for r in db.rows(con, """SELECT i.documento_id FROM trabajo_items i JOIN trabajos t ON t.id=i.trabajo_id
                                                        WHERE t.estado IN ('pendiente','en_curso') AND i.estado='pendiente'""")}


# ============================================================================ proceso en segundo plano
_evento = threading.Event()


def despertar():
    _evento.set()


def _api_key(con) -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "") or db.get_setting(con, "anthropic_api_key", "")


def _procesar_item(con, trabajo: dict, item: dict) -> None:
    from . import ingesta, lector, maestros
    doc = db.one(con, "SELECT id, filename, file_path, estado FROM documentos WHERE id=?", (item["documento_id"],))
    t0 = time.time()
    if not doc or not doc["file_path"] or not Path(doc["file_path"]).exists():
        con.execute("UPDATE trabajo_items SET estado='error', error=?, terminado_en=? WHERE id=?",
                    ("Documento o PDF no encontrado", db.now_iso(), item["id"]))
        return
    obras = maestros.listar_obras(con, solo_activas=True)
    ref = trabajo["obra_lote"] or (obras[0]["id"] if obras else None)
    partidas = maestros.partidas_de_obra(con, ref) if ref else []
    cfg = lector.config(con)
    if cfg["motor"] != "claude" and trabajo.get("modo_ia") == "nunca":
        cfg["motor"] = "local_reglas"
    elif trabajo.get("modo_ia") == "siempre":
        cfg["ia_siempre"] = True
    provs = {r["nif"]: r["nombre"] for r in db.rows(con, "SELECT nif, nombre FROM proveedores WHERE nif IS NOT NULL")
             if "LLORCA" not in (r["nombre"] or "").upper()}
    res = lector.leer(con, Path(doc["file_path"]).read_bytes(), obras, partidas, _api_key(con),
                      db.get_setting(con, "modelo", "claude-sonnet-5"), cfg, provs, doc["filename"])
    ingesta.aplicar_extraccion(con, doc["id"], res, trabajo["creado_por"] or "sistema", obra_defecto=trabajo["obra_lote"])
    d = db.one(con, "SELECT emisor_nombre, numero, base_imponible_cents, estado, confianza FROM documentos WHERE id=?", (doc["id"],))
    graves = db.one(con, "SELECT COUNT(*) n FROM incidencias WHERE documento_id=? AND resuelta=0 AND severidad IN ('critica','alta')",
                    (doc["id"],))["n"]
    estado = "duplicado" if d["estado"] == "duplicado" else ("revisar" if graves else "ok")
    resumen = {"proveedor": d["emisor_nombre"], "numero": d["numero"], "base_cents": d["base_imponible_cents"],
               "confianza": d["confianza"], "incidencias_graves": graves}
    con.execute("UPDATE trabajo_items SET estado=?, resultado=?, segundos=?, lectura=?, terminado_en=? WHERE id=?",
                (estado, json.dumps(resumen, ensure_ascii=False), round(time.time() - t0, 1), res.get("modelo"), db.now_iso(),
                 item["id"]))


def _bucle():
    con = db.connect()
    init(con)
    # trabajos interrumpidos por un reinicio: continúan
    con.execute("UPDATE trabajos SET estado='pendiente' WHERE estado='en_curso'")
    con.commit()
    # documentos anteriores a la búsqueda de texto completo: se indexan en segundo plano, sin molestar
    try:
        from . import indice
        indice.init(con)
        for did in indice.pendientes_de_indexar(con):
            indice.indexar_documento(con, did)
    except Exception:
        traceback.print_exc()
    while True:
        try:
            t = activo(con)
            if not t:
                _evento.wait(5)
                _evento.clear()
                continue
            if t["estado"] == "pendiente":
                con.execute("UPDATE trabajos SET estado='en_curso', iniciado_en=COALESCE(iniciado_en, ?) WHERE id=?", (db.now_iso(), t["id"]))
                con.commit()
            while True:
                t = db.one(con, "SELECT * FROM trabajos WHERE id=?", (t["id"],))
                if t["cancelar"]:
                    con.execute("UPDATE trabajo_items SET estado='cancelado' WHERE trabajo_id=? AND estado='pendiente'", (t["id"],))
                    con.execute("UPDATE trabajos SET estado='cancelado', terminado_en=? WHERE id=?", (db.now_iso(), t["id"]))
                    con.commit()
                    break
                it = db.one(con, "SELECT * FROM trabajo_items WHERE trabajo_id=? AND estado='pendiente' ORDER BY orden LIMIT 1", (t["id"],))
                if not it:
                    con.execute("UPDATE trabajos SET estado='terminado', terminado_en=? WHERE id=?", (db.now_iso(), t["id"]))
                    con.commit()
                    break
                con.execute("UPDATE trabajo_items SET iniciado_en=? WHERE id=?", (db.now_iso(), it["id"]))
                con.commit()
                try:
                    _procesar_item(con, t, it)
                except Exception as e:  # noqa: BLE001
                    con.rollback()
                    con.execute("UPDATE trabajo_items SET estado='error', error=?, terminado_en=? WHERE id=?",
                                (f"{type(e).__name__}: {e}"[:500], db.now_iso(), it["id"]))
                    con.execute("UPDATE trabajos SET errores=errores+1 WHERE id=?", (t["id"],))
                con.execute("UPDATE trabajos SET hechos=(SELECT COUNT(*) FROM trabajo_items WHERE trabajo_id=? AND estado<>'pendiente') "
                            "WHERE id=?", (t["id"], t["id"]))
                con.commit()
        except Exception:  # noqa: BLE001 - el proceso de fondo nunca debe morir
            traceback.print_exc()
            time.sleep(3)


_hilo = None
_lock = threading.Lock()


def arrancar() -> None:
    """Arranca (una sola vez por proceso) el lector en segundo plano."""
    global _hilo
    with _lock:
        if _hilo is None or not _hilo.is_alive():
            _hilo = threading.Thread(target=_bucle, name="lector-segundo-plano", daemon=True)
            _hilo.start()
