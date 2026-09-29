"""
COSTES Y VENTAS INTERNOS (solo para la contabilidad analítica de Llorca; el cliente nunca los ve).

Lo que no llega como factura de proveedor pero es coste o venta real de la obra:
- Costes internos: mano de obra propia (horas × coste/hora), personal técnico, maquinaria y medios propios,
  instalaciones y casetas, seguros y avales, licencias y tasas, consumos, gastos generales imputados, posventa…
- Cargos a subcontratas (repercusiones de limpieza, grúa, medios, penalizaciones): REDUCEN el coste.
- Ventas internas: obra ejecutada pendiente de certificar, revisión de precios, extras aprobados aún no certificados…

Cada movimiento lleva sus justificantes adjuntos (partes, nóminas, contratos, hojas de cálculo, fotos…), se imputa a
una obra y, opcionalmente, a un capítulo y a un oficio, y pasa por validación (Administración/Dirección).
Nada se borra: se anula, y todo queda en la auditoría.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import db
from .money import parse_amount, to_cents, q2

TIPOS = {"coste": "Coste interno", "cargo": "Cargo a subcontrata (reduce coste)", "venta": "Venta interna"}
CATEGORIAS = {
    "coste": ["Mano de obra propia", "Personal técnico de obra (jefe de obra, encargado)", "Maquinaria y medios propios",
              "Instalaciones de obra y casetas", "Seguros, avales y fianzas", "Licencias, tasas y permisos",
              "Consumos de obra (agua, luz, combustible)", "Gastos generales imputados", "Posventa y garantías",
              "Otros costes internos"],
    "cargo": ["Repercusión a subcontrata (limpieza, grúa, medios)", "Penalización a subcontrata", "Otros cargos a proveedores"],
    "venta": ["Obra ejecutada pendiente de certificar", "Revisión de precios", "Extras aprobados pendientes de certificar",
              "Otros ingresos de la obra"],
}
ES_MANO_OBRA = ("Mano de obra propia", "Personal técnico de obra (jefe de obra, encargado)")
ESTADOS = {"borrador": "Borrador", "validado": "Validado", "anulado": "Anulado"}
EXT_ADJ = ["pdf", "xlsx", "xls", "csv", "docx", "doc", "jpg", "jpeg", "png", "tif", "tiff", "txt", "msg", "eml", "zip"]
MAX_ADJ_MB = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS mov_internos (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    tipo TEXT NOT NULL, categoria TEXT NOT NULL,
    fecha TEXT NOT NULL, periodo TEXT NOT NULL,
    partida_id INTEGER REFERENCES partidas(id) ON DELETE SET NULL,
    oficio_id INTEGER,
    proveedor_id INTEGER REFERENCES proveedores(id) ON DELETE SET NULL,
    descripcion TEXT NOT NULL,
    cantidad TEXT, unidad TEXT, precio TEXT,
    importe_cents INTEGER NOT NULL,
    estado TEXT NOT NULL DEFAULT 'borrador',
    origen TEXT DEFAULT 'manual', lote TEXT,
    creado_por TEXT, creado_en TEXT, validado_por TEXT, validado_en TEXT, anulado_por TEXT, anulado_en TEXT, motivo_anulacion TEXT,
    notas TEXT
);
CREATE INDEX IF NOT EXISTS ix_mi_obra ON mov_internos(obra_id, periodo);
CREATE TABLE IF NOT EXISTS mov_adjuntos (
    id INTEGER PRIMARY KEY,
    movimiento_id INTEGER NOT NULL REFERENCES mov_internos(id) ON DELETE CASCADE,
    nombre TEXT, ruta TEXT, sha256 TEXT, bytes INTEGER, subido_por TEXT, subido_en TEXT
);
CREATE TABLE IF NOT EXISTS tarifas_mo (
    categoria TEXT PRIMARY KEY, coste_hora TEXT NOT NULL, actualizado_por TEXT, actualizado_en TEXT
);
"""

TARIFAS_INICIALES = {"Oficial 1ª": "28.50", "Oficial 2ª": "26.00", "Peón": "22.50", "Encargado": "34.00", "Jefe de obra": "42.00",
                     "Técnico": "38.00"}


def init(con):
    con.executescript(SCHEMA)
    if not db.one(con, "SELECT COUNT(*) n FROM tarifas_mo")["n"]:
        for k, v in TARIFAS_INICIALES.items():
            con.execute("INSERT INTO tarifas_mo (categoria, coste_hora, actualizado_por, actualizado_en) VALUES (?,?,?,?)",
                        (k, v, "sistema", db.now_iso()))
    con.commit()


def tarifas(con) -> dict[str, Decimal]:
    return {r["categoria"]: Decimal(r["coste_hora"]) for r in db.rows(con, "SELECT * FROM tarifas_mo ORDER BY categoria")}


def carpeta_adjuntos() -> Path:
    from .config import DATA_DIR
    p = Path(DATA_DIR) / "adjuntos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _signo(tipo: str) -> int:
    return -1 if tipo == "cargo" else 1


def crear(con, obra_id: int, tipo: str, categoria: str, fecha: str, descripcion: str, importe, usuario: str,
          partida_id=None, oficio_id=None, proveedor_id=None, cantidad=None, unidad=None, precio=None, notas=None,
          origen="manual", lote=None, periodo: str | None = None) -> int:
    if tipo not in TIPOS:
        raise ValueError("Tipo no válido.")
    if categoria not in CATEGORIAS[tipo]:
        raise ValueError("Categoría no válida para ese tipo.")
    if not str(descripcion or "").strip():
        raise ValueError("La descripción es obligatoria.")
    imp = q2(Decimal(str(importe))) if not isinstance(importe, Decimal) else q2(importe)
    if imp <= 0:
        raise ValueError("El importe debe ser positivo (el signo lo pone el tipo: los cargos restan del coste).")
    try:
        f = date.fromisoformat(str(fecha)[:10])
    except ValueError:
        raise ValueError("Fecha no válida.")
    if f > date.today().replace(year=date.today().year + 1):
        raise ValueError("La fecha es demasiado lejana: revise el año.")
    from .cierres import cerrado
    if cerrado(con, obra_id, periodo or f.isoformat()):
        raise ValueError(f"El mes {(periodo or f.isoformat())[:7]} de esta obra está cerrado: no se pueden añadir movimientos.")
    with db.tx(con):
        cur = con.execute("""INSERT INTO mov_internos (obra_id, tipo, categoria, fecha, periodo, partida_id, oficio_id, proveedor_id,
                             descripcion, cantidad, unidad, precio, importe_cents, estado, origen, lote, creado_por, creado_en, notas)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (obra_id, tipo, categoria, f.isoformat(), periodo or f.isoformat()[:7], partida_id, oficio_id, proveedor_id,
                           descripcion.strip(), None if cantidad is None else str(cantidad), unidad,
                           None if precio is None else str(precio), to_cents(imp), "borrador", origen, lote, usuario,
                           db.now_iso(), notas))
        mid = cur.lastrowid
        db.audit(con, usuario, "alta_movimiento_interno", "mov_interno", mid,
                 {"tipo": tipo, "categoria": categoria, "importe": str(imp), "obra": obra_id})
    return mid


def adjuntar(con, mov_id: int, nombre: str, data: bytes, usuario: str) -> int:
    ext = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
    if ext not in EXT_ADJ:
        raise ValueError(f"Tipo de archivo no admitido ({ext or 'sin extensión'}).")
    if not data:
        raise ValueError("Archivo vacío.")
    if len(data) > MAX_ADJ_MB * 1024 * 1024:
        raise ValueError(f"El archivo supera {MAX_ADJ_MB} MB.")
    h = hashlib.sha256(data).hexdigest()
    ya = db.one(con, "SELECT id FROM mov_adjuntos WHERE movimiento_id=? AND sha256=?", (mov_id, h))
    if ya:
        return ya["id"]
    seguro = re.sub(r"[^\w.\- ]", "_", nombre)[-120:]
    destino = carpeta_adjuntos() / h[:2] / f"{h[:16]}_{seguro}"
    destino.parent.mkdir(parents=True, exist_ok=True)
    if not destino.exists():
        destino.write_bytes(data)
    with db.tx(con):
        cur = con.execute("INSERT INTO mov_adjuntos (movimiento_id, nombre, ruta, sha256, bytes, subido_por, subido_en) VALUES (?,?,?,?,?,?,?)",
                          (mov_id, nombre, str(destino), h, len(data), usuario, db.now_iso()))
        db.audit(con, usuario, "adjuntar_justificante", "mov_interno", mov_id, {"archivo": nombre, "sha256": h})
    return cur.lastrowid


def adjuntos(con, mov_id: int) -> list[dict]:
    return db.rows(con, "SELECT * FROM mov_adjuntos WHERE movimiento_id=? ORDER BY id", (mov_id,))


def validar(con, mov_id: int, usuario: str, rol: str) -> tuple[bool, str]:
    from .cierres import cerrado as _cerr
    _m = db.one(con, "SELECT obra_id, periodo FROM mov_internos WHERE id=?", (mov_id,))
    if _m and _cerr(con, _m["obra_id"], _m["periodo"]):
        raise ValueError("El mes de este movimiento está cerrado.")
    if rol not in ("admin", "direccion", "gestor"):
        return False, "Solo Administración o Dirección pueden validar movimientos internos."
    m = db.one(con, "SELECT * FROM mov_internos WHERE id=?", (mov_id,))
    if not m or m["estado"] != "borrador":
        return False, "Solo se validan movimientos en borrador."
    if m["creado_por"] == usuario and rol != "admin":
        return False, "Un movimiento no lo valida quien lo crea (principio de los cuatro ojos)."
    if db.get_setting(con, "internos_adjunto_obligatorio", "1") == "1" and not adjuntos(con, mov_id):
        return False, "Falta el justificante adjunto (parte, nómina, contrato, hoja de cálculo…)."
    with db.tx(con):
        con.execute("UPDATE mov_internos SET estado='validado', validado_por=?, validado_en=? WHERE id=?", (usuario, db.now_iso(), mov_id))
        db.audit(con, usuario, "validar_movimiento_interno", "mov_interno", mov_id, None)
    return True, "Validado"


def anular(con, mov_id: int, usuario: str, motivo: str) -> None:
    from .cierres import cerrado as _cerr
    _m = db.one(con, "SELECT obra_id, periodo FROM mov_internos WHERE id=?", (mov_id,))
    if _m and _cerr(con, _m["obra_id"], _m["periodo"]):
        raise ValueError("El mes de este movimiento está cerrado.")
    if not (motivo or "").strip():
        raise ValueError("Indique el motivo de la anulación.")
    with db.tx(con):
        con.execute("UPDATE mov_internos SET estado='anulado', anulado_por=?, anulado_en=?, motivo_anulacion=? WHERE id=?",
                    (usuario, db.now_iso(), motivo.strip(), mov_id))
        db.audit(con, usuario, "anular_movimiento_interno", "mov_interno", mov_id, {"motivo": motivo})


def reactivar(con, mov_id: int, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE mov_internos SET estado='borrador', anulado_por=NULL, anulado_en=NULL, motivo_anulacion=NULL, "
                    "validado_por=NULL, validado_en=NULL WHERE id=? AND estado='anulado'", (mov_id,))
        db.audit(con, usuario, "reactivar_movimiento_interno", "mov_interno", mov_id, None)


def modificar(con, mov_id: int, cambios: dict, usuario: str) -> None:
    from .cierres import cerrado as _cerr
    _m = db.one(con, "SELECT obra_id, periodo FROM mov_internos WHERE id=?", (mov_id,))
    if _m and _cerr(con, _m["obra_id"], _m["periodo"]):
        raise ValueError("El mes de este movimiento está cerrado.")
    """Editar un movimiento; si estaba validado vuelve a borrador (la validación ya no vale)."""
    permitidos = {"categoria", "fecha", "periodo", "partida_id", "oficio_id", "proveedor_id", "descripcion", "cantidad", "unidad",
                  "precio", "importe_cents", "notas"}
    antes = db.one(con, "SELECT * FROM mov_internos WHERE id=?", (mov_id,))
    diff = {k: {"antes": antes[k], "despues": v} for k, v in cambios.items() if k in permitidos and antes[k] != v}
    if not diff:
        return
    with db.tx(con):
        con.execute(f"UPDATE mov_internos SET {', '.join(k + '=?' for k in diff)}, estado=CASE WHEN estado='validado' THEN 'borrador' "
                    f"ELSE estado END, validado_por=NULL, validado_en=NULL WHERE id=?", [cambios[k] for k in diff] + [mov_id])
        db.audit(con, usuario, "editar_movimiento_interno", "mov_interno", mov_id, diff)


def movimientos(con, obra_id: int | None = None, desde: str | None = None, hasta: str | None = None,
                incluir_borradores: bool = True) -> list[dict]:
    q = """SELECT m.*, o.codigo AS obra, p.codigo AS capitulo, pr.nombre AS proveedor,
                  (SELECT COUNT(*) FROM mov_adjuntos a WHERE a.movimiento_id=m.id) AS n_adjuntos
           FROM mov_internos m JOIN obras o ON o.id=m.obra_id LEFT JOIN partidas p ON p.id=m.partida_id
           LEFT JOIN proveedores pr ON pr.id=m.proveedor_id WHERE m.estado<>'anulado'"""
    p = []
    if not incluir_borradores:
        q += " AND m.estado='validado'"
    if obra_id:
        q += " AND m.obra_id=?"; p.append(obra_id)
    if desde:
        q += " AND m.fecha>=?"; p.append(desde)
    if hasta:
        q += " AND m.fecha<=?"; p.append(hasta)
    return db.rows(con, q + " ORDER BY m.fecha DESC, m.id DESC", p)


def resumen(con, obra_id: int, desde: str | None = None, hasta: str | None = None, incluir_borradores: bool = True) -> dict:
    """Totales con signo: costes (+), cargos a subcontratas (−) y ventas internas; mano de obra aparte."""
    r = {"coste": Decimal(0), "mano_obra": Decimal(0), "cargos": Decimal(0), "venta": Decimal(0), "n": 0,
         "por_partida_coste": {}, "por_partida_venta": {}, "por_oficio_coste": {}, "por_oficio_venta": {}, "borradores": 0}
    for m in movimientos(con, obra_id, desde, hasta, incluir_borradores):
        imp = Decimal(m["importe_cents"]) / 100
        r["n"] += 1
        r["borradores"] += m["estado"] == "borrador"
        if m["tipo"] == "venta":
            r["venta"] += imp
            r["por_partida_venta"][m["partida_id"] or -1] = r["por_partida_venta"].get(m["partida_id"] or -1, Decimal(0)) + imp
            if m["oficio_id"]:
                r["por_oficio_venta"][m["oficio_id"]] = r["por_oficio_venta"].get(m["oficio_id"], Decimal(0)) + imp
            continue
        v = imp * _signo(m["tipo"])
        if m["tipo"] == "cargo":
            r["cargos"] += imp
        elif m["categoria"] in ES_MANO_OBRA:
            r["mano_obra"] += imp
        else:
            r["coste"] += imp
        r["por_partida_coste"][m["partida_id"] or -1] = r["por_partida_coste"].get(m["partida_id"] or -1, Decimal(0)) + v
        if m["oficio_id"]:
            r["por_oficio_coste"][m["oficio_id"]] = r["por_oficio_coste"].get(m["oficio_id"], Decimal(0)) + v
    r["coste_neto"] = r["coste"] + r["mano_obra"] - r["cargos"]
    return r


# ============================================================================ reparto de gastos generales
def repartir_gastos_generales(con, importe, periodo: str, criterio: str, obras: list[int], descripcion: str, usuario: str,
                              categoria: str = "Gastos generales imputados") -> list[tuple[int, Decimal]]:
    """Reparte un gasto general del mes entre obras: por venta certificada del mes, por coste del mes o a partes iguales.
    Genera un movimiento por obra (mismo lote) con el cálculo del reparto en las notas; el último absorbe el redondeo."""
    total = q2(Decimal(str(importe)))
    if total <= 0 or not obras:
        raise ValueError("Indique un importe positivo y al menos una obra.")
    pesos = {}
    for o in obras:
        if criterio == "venta_mes":
            r = db.one(con, "SELECT SUM(total_actual_m) s FROM certificaciones WHERE obra_id=? AND substr(fecha,1,7)=?", (o, periodo))
            pesos[o] = Decimal((r and r["s"]) or 0) / 1000
        elif criterio == "coste_mes":
            r = db.one(con, """SELECT SUM(base_imponible_cents) s FROM documentos WHERE obra_id=? AND substr(fecha,1,7)=?
                               AND estado NOT IN ('rechazada','eliminado','duplicado') AND tipo_documento IN ('factura','abono','anticipo')""",
                       (o, periodo))
            pesos[o] = Decimal((r and r["s"]) or 0) / 100
        else:
            pesos[o] = Decimal(1)
    suma = sum(pesos.values())
    if suma <= 0:
        raise ValueError("Ninguna obra tiene " + ("venta certificada" if criterio == "venta_mes" else "coste") +
                         f" en {periodo}: elija otro criterio o reparta a partes iguales.")
    lote = f"GG-{periodo}-{datetime.now():%H%M%S}"
    fecha = f"{periodo}-28"
    out, acumulado = [], Decimal(0)
    activas = [o for o in obras if pesos[o] > 0]
    for i, o in enumerate(activas):
        parte = total - acumulado if i == len(activas) - 1 else q2(total * pesos[o] / suma)
        acumulado += parte
        if parte <= 0:
            continue
        mid = crear(con, o, "coste", categoria, fecha, descripcion, parte, usuario, origen="reparto", lote=lote, periodo=periodo,
                    notas=f"Reparto de {total} € por {criterio.replace('_', ' ')}: peso {pesos[o]:.2f} de {suma:.2f} "
                          f"({(pesos[o] / suma * 100):.2f} %).")
        out.append((mid, parte))
    return out


# ============================================================================ importación de partes de mano de obra
def importar_partes(con, data: bytes, nombre: str, obra_defecto: int | None, usuario: str) -> dict:
    """Excel/CSV de partes: fecha, trabajador, categoría, horas, [coste_hora], [obra], [capítulo], [descripción].
    Si falta el coste/hora se toma la tarifa de su categoría. Crea un movimiento por obra, capítulo y día (agrupado)."""
    import pandas as pd
    if nombre.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(data), sep=None, engine="python", dtype=str)
    else:
        df = pd.read_excel(io.BytesIO(data), dtype=str)
    cols = {c: re.sub(r"[^a-z]", "", str(c).lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o")
                      .replace("ú", "u")) for c in df.columns}
    def col(*claves):
        return next((c for c, n in cols.items() if any(n.startswith(k) for k in claves)), None)
    c_f, c_t, c_cat, c_h = col("fecha", "dia"), col("trabajador", "operario", "nombre", "empleado"), col("categoria", "puesto"), col("horas", "h")
    c_p, c_o, c_cap, c_d = col("costehora", "precio", "tarifa"), col("obra"), col("capitulo", "partida"), col("descripcion", "tarea", "trabajo")
    if not (c_f and c_h):
        raise ValueError("El archivo debe tener al menos las columnas «fecha» y «horas».")
    tar = tarifas(con)
    obras = {o["codigo"]: o["id"] for o in db.rows(con, "SELECT id, codigo FROM obras")}
    errores, grupos = [], {}
    for i, r in df.iterrows():
        try:
            f = pd.to_datetime(r[c_f], dayfirst=True).date()
            h = parse_amount(str(r[c_h]))
            if h <= 0 or h > 24:
                raise ValueError(f"horas fuera de rango ({h})")
            cat = str(r[c_cat]).strip() if c_cat and str(r[c_cat]) != "nan" else "Oficial 1ª"
            ph = parse_amount(str(r[c_p])) if c_p and str(r[c_p]) not in ("nan", "") else tar.get(cat)
            if ph is None or ph <= 0:
                raise ValueError(f"sin coste/hora para la categoría «{cat}» (añádala en Tarifas)")
            ob = obras.get(str(r[c_o]).strip()) if c_o and str(r[c_o]) != "nan" else obra_defecto
            if not ob:
                raise ValueError("obra no indicada o inexistente")
            cap = str(r[c_cap]).strip() if c_cap and str(r[c_cap]) != "nan" else None
            pid = None
            if cap:
                pr = db.one(con, "SELECT id FROM partidas WHERE obra_id=? AND codigo=?", (ob, cap))
                pid = pr and pr["id"]
            k = (ob, pid, f.isoformat())
            g = grupos.setdefault(k, {"horas": Decimal(0), "importe": Decimal(0), "detalle": []})
            g["horas"] += h
            g["importe"] += q2(h * ph)
            nom = str(r[c_t]).strip() if c_t and str(r[c_t]) != "nan" else "sin nombre"
            g["detalle"].append(f"{nom} ({cat}) {h} h × {ph} €")
        except Exception as e:  # noqa: BLE001
            errores.append(f"Fila {i + 2}: {e}")
    lote = f"PARTES-{datetime.now():%Y%m%d%H%M%S}"
    creados = 0
    for (ob, pid, f), g in grupos.items():
        mid = crear(con, ob, "coste", "Mano de obra propia", f, f"Partes de trabajo del {f}: {len(g['detalle'])} registro(s)",
                    g["importe"], usuario, partida_id=pid, cantidad=g["horas"], unidad="h", origen="importacion", lote=lote,
                    notas="; ".join(g["detalle"])[:4000])
        adjuntar(con, mid, nombre, data, usuario)
        creados += 1
    return {"movimientos": creados, "filas": len(df), "errores": errores, "lote": lote}
