"""
CIERRE MENSUAL DE OBRA con bloqueo del periodo.

Antes de llevar el resultado de una obra a la cuenta de resultados, se revisa una lista de comprobación (certificación del
mes, facturas aprobadas, incidencias, costes internos validados, excepciones, provisión, factura al cliente). Al cerrar:
- se congelan las cifras del mes (foto del resultado) y se guarda en el histórico;
- el periodo queda BLOQUEADO: ninguna factura ni movimiento interno de esa obra con fecha de ese mes puede modificarse,
  aprobarse, rechazarse ni releerse hasta que Dirección o Administración reabran el mes (con motivo, y queda registrado).
"""
from __future__ import annotations

import json
from datetime import date

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS cierres (
    id INTEGER PRIMARY KEY, obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE, periodo TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'cerrado', cerrado_por TEXT, cerrado_en TEXT, comentario TEXT, foto TEXT, avisos TEXT,
    reabierto_por TEXT, reabierto_en TEXT, motivo_reapertura TEXT, UNIQUE (obra_id, periodo));
"""


class PeriodoCerrado(ValueError):
    pass


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def cerrado(con, obra_id: int | None, fecha: str | None) -> bool:
    if not obra_id or not fecha:
        return False
    try:
        r = db.one(con, "SELECT 1 x FROM cierres WHERE obra_id=? AND periodo=? AND estado='cerrado'", (obra_id, str(fecha)[:7]))
    except Exception:
        return False
    return bool(r)


def comprobar_documento(con, doc_id: int) -> None:
    d = db.one(con, "SELECT obra_id, fecha FROM documentos WHERE id=?", (doc_id,))
    if d and cerrado(con, d["obra_id"], d["fecha"]):
        raise PeriodoCerrado(f"El mes {str(d['fecha'])[:7]} de esta obra está CERRADO: no se puede modificar. "
                             "Si es imprescindible, Dirección o Administración deben reabrirlo en «Cierre mensual».")


def lista_comprobacion(con, obra_id: int, periodo: str) -> list[dict]:
    """Cada punto: ok (True/False), grave (bloquea el cierre salvo confirmación expresa) y detalle."""
    ini, fin = f"{periodo}-01", f"{periodo}-31"
    out = []

    def add(nombre, ok, detalle, grave=True):
        out.append({"punto": nombre, "ok": bool(ok), "grave": grave, "detalle": detalle})
    cert = db.one(con, "SELECT numero, fecha, total_actual_m FROM certificaciones WHERE obra_id=? AND substr(fecha,1,7)=? ORDER BY numero DESC LIMIT 1",
                  (obra_id, periodo))
    add("Certificación del mes importada", cert, f"nº {cert['numero']} del {cert['fecha']}" if cert else "No hay certificación con fecha de este mes.")
    pend = db.one(con, """SELECT COUNT(*) n FROM documentos WHERE obra_id=? AND fecha BETWEEN ? AND ? AND estado IN ('sin_procesar','pendiente_revision','revisada')
                          AND tipo_documento IN ('factura','abono','anticipo')""", (obra_id, ini, fin))["n"]
    add("Facturas del mes aprobadas o rechazadas", pend == 0, f"{pend} factura(s) del mes aún sin aprobar." if pend else "Todas resueltas.")
    crit = db.one(con, """SELECT COUNT(*) n FROM incidencias i JOIN documentos d ON d.id=i.documento_id WHERE d.obra_id=? AND d.fecha BETWEEN ? AND ?
                          AND i.resuelta=0 AND i.severidad='critica' AND d.estado NOT IN ('rechazada','eliminado','duplicado')""", (obra_id, ini, fin))["n"]
    add("Sin incidencias críticas abiertas", crit == 0, f"{crit} incidencia(s) crítica(s) abiertas." if crit else "Ninguna.")
    try:
        bor = db.one(con, "SELECT COUNT(*) n FROM mov_internos WHERE obra_id=? AND periodo=? AND estado='borrador'", (obra_id, periodo))["n"]
    except Exception:
        bor = 0
    add("Costes y ventas internos validados", bor == 0, f"{bor} movimiento(s) interno(s) en borrador." if bor else "Todos validados.")
    try:
        from .casacion import casar
        exc = [x for x in casar(con, obra_id) if not x["casada"] and str(x["fecha"] or "")[:7] == periodo]
    except Exception:
        exc = []
    add("Casación de subcontratas sin excepciones", not exc, f"{len(exc)} excepción(es) de casación en el mes." if exc else "Sin excepciones.", grave=False)
    try:
        from .obra_control import devengado_pendiente
        dev = devengado_pendiente(con, obra_id, fin)
    except Exception:
        dev = []
    add("Coste ejecutado sin factura identificado (provisión)", True,
        f"{len(dev)} certificación(es)/albarán(es) de proveedor sin factura: se provisionan." if dev else "No hay coste pendiente de factura.", grave=False)
    if cert:
        try:
            fe = db.one(con, "SELECT codigo, estado FROM facturas_emitidas WHERE cert_id=(SELECT id FROM certificaciones WHERE obra_id=? AND numero=?) "
                             "AND estado<>'anulada'", (obra_id, cert["numero"]))
        except Exception:
            fe = None
        add("Certificación facturada al cliente", fe and fe["estado"] == "emitida",
            f"Factura {fe['codigo'] or 'en borrador'}" if fe else "La certificación del mes aún no tiene factura emitida.", grave=False)
    return out


def cerrar(con, obra_id: int, periodo: str, usuario: str, rol: str, comentario: str = "", forzar: bool = False) -> int:
    if rol not in ("admin", "direccion", "gestor"):
        raise ValueError("Solo Administración o Dirección cierran meses.")
    if periodo >= date.today().strftime("%Y-%m") and not forzar:
        raise ValueError("El mes aún no ha terminado. Márquelo como cierre anticipado si es intencionado.")
    lc = lista_comprobacion(con, obra_id, periodo)
    graves = [x for x in lc if x["grave"] and not x["ok"]]
    if graves and not (forzar and comentario.strip()):
        raise ValueError("Hay puntos pendientes: " + "; ".join(f"{x['punto']} ({x['detalle']})" for x in graves) +
                         ". Resuélvalos o cierre de todos modos indicando el motivo.")
    foto = {}
    try:
        from . import auditoria as A
        cert = db.one(con, "SELECT id FROM certificaciones WHERE obra_id=? AND substr(fecha,1,7)=? ORDER BY numero DESC LIMIT 1", (obra_id, periodo))
        if cert:
            for modo in ("mes", "origen"):
                r = A.calcular(con, obra_id, cert["id"], None, False, modo, True)
                foto[modo] = {k: str(r[k]) for k in ("ventas", "compras", "rrhh", "resultado_sis", "correccion", "resultado", "pct")}
            A.guardar_mes(con, obra_id, A.calcular(con, obra_id, cert["id"], None, False, "origen", True), usuario)
    except Exception as e:  # noqa: BLE001
        foto["error"] = str(e)
    with db.tx(con):
        con.execute("""INSERT INTO cierres (obra_id, periodo, estado, cerrado_por, cerrado_en, comentario, foto, avisos) VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(obra_id, periodo) DO UPDATE SET estado='cerrado', cerrado_por=excluded.cerrado_por, cerrado_en=excluded.cerrado_en,
                       comentario=excluded.comentario, foto=excluded.foto, avisos=excluded.avisos""",
                    (obra_id, periodo, "cerrado", usuario, db.now_iso(), comentario, json.dumps(foto, ensure_ascii=False),
                     json.dumps([x for x in lc if not x["ok"]], ensure_ascii=False)))
        db.audit(con, usuario, "cerrar_mes", "obra", obra_id, {"periodo": periodo, "forzado": bool(graves), "comentario": comentario})
    return db.one(con, "SELECT id FROM cierres WHERE obra_id=? AND periodo=?", (obra_id, periodo))["id"]


def reabrir(con, obra_id: int, periodo: str, usuario: str, rol: str, motivo: str) -> None:
    if rol not in ("admin", "direccion"):
        raise ValueError("Solo Dirección o el administrador reabren un mes cerrado.")
    if not motivo.strip():
        raise ValueError("Indique el motivo de la reapertura.")
    with db.tx(con):
        con.execute("UPDATE cierres SET estado='reabierto', reabierto_por=?, reabierto_en=?, motivo_reapertura=? WHERE obra_id=? AND periodo=?",
                    (usuario, db.now_iso(), motivo, obra_id, periodo))
        db.audit(con, usuario, "reabrir_mes", "obra", obra_id, {"periodo": periodo, "motivo": motivo})


def listado(con, obra_id: int | None = None) -> list[dict]:
    q = "SELECT c.*, o.codigo AS obra FROM cierres c JOIN obras o ON o.id=c.obra_id"
    return db.rows(con, q + (" WHERE c.obra_id=?" if obra_id else "") + " ORDER BY c.periodo DESC", (obra_id,) if obra_id else ())
