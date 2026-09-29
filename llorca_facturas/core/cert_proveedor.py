"""
CERTIFICACIÓN A PROVEEDOR (subcontratistas) Y AUTORIZACIÓN DE FACTURACIÓN.

Cierra el ciclo del coste: la obra mide lo que ha hecho el subcontratista, se le certifica y se le pide factura SOLO por
lo aprobado. Así la factura que llegue ya casa con una certificación propia (casación triple) y no hay sorpresas.

1. Líneas de contrato de cada oferta adjudicada: código, descripción, unidad, cantidad contratada y precio (desde Excel,
   desde el estudio de ofertas o a mano), y la partida de la certificación al cliente a la que corresponde.
2. Certificación mensual correlativa por contrato: se parte de lo certificado a origen hasta el mes anterior; el jefe de
   obra mide a origen (o el mes) y el sistema calcula anterior, mes, % y euros.
   - Medir por encima de lo contratado es un EXCESO: no se puede aprobar sin marcar que hay orden de cambio aprobada.
   - Medir a origen por debajo de lo ya certificado (descertificar) se avisa.
3. Aprobación por Administración o Dirección (cuatro ojos: no la aprueba quien midió, salvo el administrador).
4. Al aprobar se genera el PDF «Certificación nº N · Autorización de facturación» (base, retención de garantía, IVA o
   inversión del sujeto pasivo, líquido) y se deja preparado el correo al subcontratista con el PDF adjunto.
5. Cuando llega su factura (buzón o subida) se casa sola con la certificación aprobada del mismo importe (±1 %) y la
   certificación pasa a «facturada». Lo aprobado sin factura es coste devengado (provisión de cierre).
"""
from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from . import db
from .config import DATA_DIR
from .money import D, q2, to_cents, fmt_eur, parse_amount

SCHEMA = """
CREATE TABLE IF NOT EXISTS contrato_lineas (
    id INTEGER PRIMARY KEY, oferta_id INTEGER NOT NULL REFERENCES ofertas(id) ON DELETE CASCADE,
    orden INTEGER, codigo TEXT, descripcion TEXT NOT NULL, unidad TEXT, cantidad TEXT NOT NULL, precio TEXT NOT NULL,
    importe_cents INTEGER NOT NULL, cert_partida TEXT, activa INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_cl_oferta ON contrato_lineas(oferta_id);
CREATE TABLE IF NOT EXISTS contrato_condiciones (
    oferta_id INTEGER PRIMARY KEY REFERENCES ofertas(id) ON DELETE CASCADE,
    ret_pct TEXT DEFAULT '5', iva_pct TEXT DEFAULT '21', isp INTEGER DEFAULT 1, forma_pago TEXT, email TEXT, notas TEXT);
CREATE TABLE IF NOT EXISTS certs_proveedor (
    id INTEGER PRIMARY KEY, oferta_id INTEGER NOT NULL REFERENCES ofertas(id) ON DELETE CASCADE,
    obra_id INTEGER NOT NULL, proveedor_id INTEGER, numero INTEGER NOT NULL, periodo TEXT NOT NULL, fecha TEXT,
    estado TEXT NOT NULL DEFAULT 'borrador', exceso_autorizado INTEGER DEFAULT 0, exceso_referencia TEXT,
    base_origen_cents INTEGER DEFAULT 0, base_anterior_cents INTEGER DEFAULT 0, base_mes_cents INTEGER DEFAULT 0,
    ret_pct TEXT, ret_cents INTEGER DEFAULT 0, iva_pct TEXT, iva_cents INTEGER DEFAULT 0, isp INTEGER DEFAULT 0,
    total_cents INTEGER DEFAULT 0, liquido_cents INTEGER DEFAULT 0,
    medido_por TEXT, medido_en TEXT, aprobado_por TEXT, aprobado_en TEXT, anulado_por TEXT, motivo_anulacion TEXT,
    documento_factura_id INTEGER, pdf_path TEXT, correo_id INTEGER, notas TEXT,
    UNIQUE (oferta_id, numero));
CREATE INDEX IF NOT EXISTS ix_cp_obra ON certs_proveedor(obra_id, periodo);
CREATE TABLE IF NOT EXISTS cert_prov_lineas (
    id INTEGER PRIMARY KEY, cert_id INTEGER NOT NULL REFERENCES certs_proveedor(id) ON DELETE CASCADE,
    linea_id INTEGER NOT NULL REFERENCES contrato_lineas(id), cant_anterior TEXT, cant_origen TEXT, cant_mes TEXT,
    precio TEXT, importe_origen_cents INTEGER, importe_anterior_cents INTEGER, importe_mes_cents INTEGER, observaciones TEXT,
    UNIQUE (cert_id, linea_id));
"""
ESTADOS = {"borrador": "En medición", "aprobada": "Aprobada (pedir factura)", "facturada": "Facturada", "anulada": "Anulada"}
TOL_CASE = Decimal("0.01")


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def carpeta() -> Path:
    p = DATA_DIR / "cert_proveedor"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _q3(v) -> Decimal:
    return D(v).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _dec(v) -> Decimal:
    if v is None or v == "":
        return Decimal(0)
    if isinstance(v, (int, float, Decimal)):
        return D(v)
    return parse_amount(str(v))


# ============================================================================ contratos
def contratos(con, obra_id: int | None = None) -> list[dict]:
    q = """SELECT o.id AS oferta_id, o.obra_id, ob.codigo AS obra, o.proveedor_id, COALESCE(pr.nombre, o.proveedor_nombre) AS proveedor,
                  o.alcance, o.importe_cents, o.cert_partida, p.codigo AS partida_codigo, p.descripcion AS partida,
                  (SELECT COUNT(*) FROM contrato_lineas l WHERE l.oferta_id=o.id AND l.activa=1) AS n_lineas,
                  (SELECT COALESCE(SUM(importe_cents),0) FROM contrato_lineas l WHERE l.oferta_id=o.id AND l.activa=1) AS lineas_cents
           FROM ofertas o JOIN obras ob ON ob.id=o.obra_id LEFT JOIN proveedores pr ON pr.id=o.proveedor_id LEFT JOIN partidas p ON p.id=o.partida_id
           WHERE o.estado='adjudicada'"""
    p = []
    if obra_id:
        q += " AND o.obra_id=?"; p.append(obra_id)
    out = db.rows(con, q + " ORDER BY ob.codigo, proveedor", p)
    for c in out:
        u = ultima(con, c["oferta_id"])
        c["certificado_origen_cents"] = u["base_origen_cents"] if u else 0
        c["ultima_cert"] = u["numero"] if u else None
    return out


def condiciones(con, oferta_id: int) -> dict:
    r = db.one(con, "SELECT * FROM contrato_condiciones WHERE oferta_id=?", (oferta_id,))
    if r:
        return r
    return {"oferta_id": oferta_id, "ret_pct": db.get_setting(con, "cp_ret_defecto", "5"), "iva_pct": "21", "isp": 1,
            "forma_pago": db.get_setting(con, "cp_forma_pago", "Transferencia a 60 días"), "email": "", "notas": ""}


def guardar_condiciones(con, oferta_id: int, datos: dict, usuario: str) -> None:
    c = {**condiciones(con, oferta_id), **datos}
    for k in ("ret_pct", "iva_pct"):
        c[k] = str(_dec(c[k]))
    if c.get("email"):
        from .correo import validar_direcciones
        if validar_direcciones(c["email"]):
            raise ValueError("El correo del subcontratista no es válido.")
    with db.tx(con):
        con.execute("""INSERT INTO contrato_condiciones (oferta_id, ret_pct, iva_pct, isp, forma_pago, email, notas) VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(oferta_id) DO UPDATE SET ret_pct=excluded.ret_pct, iva_pct=excluded.iva_pct, isp=excluded.isp,
                       forma_pago=excluded.forma_pago, email=excluded.email, notas=excluded.notas""",
                    (oferta_id, c["ret_pct"], c["iva_pct"], int(bool(c["isp"])), c.get("forma_pago"), c.get("email"), c.get("notas")))
        db.audit(con, usuario, "condiciones_contrato", "oferta", oferta_id, {k: c.get(k) for k in ("ret_pct", "iva_pct", "isp")})


def lineas(con, oferta_id: int, solo_activas: bool = True) -> list[dict]:
    return db.rows(con, "SELECT * FROM contrato_lineas WHERE oferta_id=?" + (" AND activa=1" if solo_activas else "") + " ORDER BY orden, id",
                   (oferta_id,))


def guardar_lineas(con, oferta_id: int, filas: list[dict], usuario: str) -> dict:
    """Sustituye las líneas del contrato. Las ya certificadas no se borran: se desactivan (su histórico se conserva)."""
    usadas = {r["linea_id"] for r in db.rows(con, """SELECT DISTINCT l.linea_id FROM cert_prov_lineas l JOIN certs_proveedor c ON c.id=l.cert_id
                                                    WHERE c.oferta_id=? AND c.estado<>'anulada'""", (oferta_id,))}
    limpias, errores = [], []
    for i, f in enumerate(filas, 1):
        desc = str(f.get("descripcion") or "").strip()
        if not desc:
            continue
        try:
            cant, precio = _dec(f.get("cantidad")), _dec(f.get("precio"))
        except Exception:
            errores.append(f"Fila {i}: cantidad o precio no válidos")
            continue
        limpias.append({"id": f.get("id"), "codigo": str(f.get("codigo") or "").strip() or None, "descripcion": desc,
                        "unidad": str(f.get("unidad") or "").strip() or None, "cantidad": str(cant), "precio": str(precio),
                        "importe_cents": to_cents(cant * precio), "cert_partida": str(f.get("cert_partida") or "").strip() or None})
    if errores:
        raise ValueError("; ".join(errores))
    actuales = {r["id"]: r for r in lineas(con, oferta_id, solo_activas=False)}
    with db.tx(con):
        vistos = set()
        for n, f in enumerate(limpias):
            lid = int(f["id"]) if f.get("id") not in (None, "") and str(f["id"]).isdigit() and int(f["id"]) in actuales else None
            if lid:
                con.execute("""UPDATE contrato_lineas SET orden=?, codigo=?, descripcion=?, unidad=?, cantidad=?, precio=?, importe_cents=?,
                               cert_partida=?, activa=1 WHERE id=?""",
                            (n, f["codigo"], f["descripcion"], f["unidad"], f["cantidad"], f["precio"], f["importe_cents"], f["cert_partida"], lid))
                vistos.add(lid)
            else:
                cur = con.execute("""INSERT INTO contrato_lineas (oferta_id, orden, codigo, descripcion, unidad, cantidad, precio, importe_cents, cert_partida)
                                     VALUES (?,?,?,?,?,?,?,?,?)""",
                                  (oferta_id, n, f["codigo"], f["descripcion"], f["unidad"], f["cantidad"], f["precio"], f["importe_cents"], f["cert_partida"]))
                vistos.add(cur.lastrowid)
        for lid in set(actuales) - vistos:
            if lid in usadas:
                con.execute("UPDATE contrato_lineas SET activa=0 WHERE id=?", (lid,))
            else:
                con.execute("DELETE FROM contrato_lineas WHERE id=?", (lid,))
        db.audit(con, usuario, "lineas_contrato", "oferta", oferta_id, {"lineas": len(limpias)})
    of = db.one(con, "SELECT importe_cents FROM ofertas WHERE id=?", (oferta_id,))
    tot = sum(f["importe_cents"] for f in limpias)
    return {"lineas": len(limpias), "total_cents": tot, "oferta_cents": of["importe_cents"] if of else 0,
            "diferencia_cents": tot - (of["importe_cents"] if of else 0)}


def leer_excel_lineas(data: bytes, nombre: str = "") -> list[dict]:
    """Excel/CSV con columnas código, descripción, unidad, cantidad, precio y (opcional) partida de la certificación."""
    import pandas as pd
    if nombre.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(data), sep=None, engine="python", dtype=str)
    else:
        df = pd.read_excel(io.BytesIO(data), dtype=str)
    norm = {c: re.sub(r"[^a-z]", "", str(c).lower().replace("ó", "o").replace("í", "i").replace("é", "e")) for c in df.columns}
    alias = {"codigo": ("codigo", "cod", "code"), "descripcion": ("descripcion", "concepto", "partida", "resumen", "texto"),
             "unidad": ("unidad", "ud", "uds", "und"), "cantidad": ("cantidad", "medicion", "cant", "med"),
             "precio": ("precio", "preciounitario", "pu", "punit"), "cert_partida": ("partidacertificacion", "certpartida", "partidacliente", "codigocliente")}
    col = {}
    for k, opciones in alias.items():
        for c, n in norm.items():
            if n in opciones and c not in col.values():
                col[k] = c
                break
    if "descripcion" not in col or "cantidad" not in col or "precio" not in col:
        raise ValueError("La hoja debe tener al menos columnas de descripción, cantidad y precio.")
    out = []
    for r in df.fillna("").to_dict("records"):
        out.append({k: r.get(c, "") for k, c in col.items()})
    return [r for r in out if str(r.get("descripcion", "")).strip() and str(r.get("cantidad", "")).strip()]


def lineas_desde_estudio(con, solicitud_id: int) -> list[dict]:
    """Líneas de contrato a partir de la oferta ganadora del estudio (separata rellenada)."""
    return [{"codigo": r["codigo"], "descripcion": r["descripcion"], "unidad": r["unidad"], "cantidad": r["medicion"], "precio": r["precio"]}
            for r in db.rows(con, """SELECT p.codigo, p.descripcion, p.unidad, p.medicion, ep.precio FROM estudio_precios ep
                                     JOIN estudio_partidas p ON p.id=ep.partida_id WHERE ep.solicitud_id=? ORDER BY p.orden""", (solicitud_id,))]


# ============================================================================ certificaciones
def ultima(con, oferta_id: int, estados=("aprobada", "facturada"), hasta_periodo: str | None = None) -> dict | None:
    q = f"SELECT * FROM certs_proveedor WHERE oferta_id=? AND estado IN ({','.join('?' * len(estados))})"
    p = [oferta_id, *estados]
    if hasta_periodo:
        q += " AND periodo<=?"; p.append(hasta_periodo)
    return db.one(con, q + " ORDER BY numero DESC LIMIT 1", p)


def listado(con, obra_id: int | None = None, oferta_id: int | None = None, periodo: str | None = None) -> list[dict]:
    q = """SELECT c.*, COALESCE(pr.nombre, o.proveedor_nombre) AS proveedor, ob.codigo AS obra, d.numero AS factura_numero
           FROM certs_proveedor c JOIN ofertas o ON o.id=c.oferta_id JOIN obras ob ON ob.id=c.obra_id
           LEFT JOIN proveedores pr ON pr.id=c.proveedor_id LEFT JOIN documentos d ON d.id=c.documento_factura_id WHERE 1=1"""
    p = []
    for campo, v in (("c.obra_id", obra_id), ("c.oferta_id", oferta_id), ("c.periodo", periodo)):
        if v:
            q += f" AND {campo}=?"; p.append(v)
    return db.rows(con, q + " ORDER BY c.periodo DESC, proveedor, c.numero DESC", p)


def nueva(con, oferta_id: int, periodo: str, fecha: str, usuario: str) -> int:
    of = db.one(con, "SELECT * FROM ofertas WHERE id=?", (oferta_id,))
    if not of or of["estado"] != "adjudicada":
        raise ValueError("Solo se certifica sobre un contrato (oferta adjudicada).")
    if not re.match(r"^\d{4}-\d{2}$", periodo or ""):
        raise ValueError("El periodo debe ser AAAA-MM.")
    ls = lineas(con, oferta_id)
    if not ls:
        raise ValueError("El contrato no tiene líneas. Cárguelas antes de certificar.")
    if db.one(con, "SELECT id FROM certs_proveedor WHERE oferta_id=? AND estado='borrador'", (oferta_id,)):
        raise ValueError("Ya hay una certificación en medición para este contrato: termínela o anúlela.")
    if db.one(con, "SELECT id FROM certs_proveedor WHERE oferta_id=? AND periodo=? AND estado<>'anulada'", (oferta_id, periodo)):
        raise ValueError(f"Ya existe una certificación de {periodo} para este contrato.")
    ult = ultima(con, oferta_id)
    if ult and ult["periodo"] > periodo:
        raise ValueError(f"La última certificación aprobada es de {ult['periodo']}: no se puede certificar un mes anterior.")
    previas = {}
    if ult:
        previas = {r["linea_id"]: r for r in db.rows(con, "SELECT * FROM cert_prov_lineas WHERE cert_id=?", (ult["id"],))}
    num = ((db.one(con, "SELECT MAX(numero) n FROM certs_proveedor WHERE oferta_id=?", (oferta_id,)) or {}).get("n") or 0) + 1
    cond = condiciones(con, oferta_id)
    with db.tx(con):
        cur = con.execute("""INSERT INTO certs_proveedor (oferta_id, obra_id, proveedor_id, numero, periodo, fecha, estado, ret_pct, iva_pct, isp,
                             medido_por, medido_en) VALUES (?,?,?,?,?,?,'borrador',?,?,?,?,?)""",
                          (oferta_id, of["obra_id"], of["proveedor_id"], num, periodo, fecha, cond["ret_pct"], cond["iva_pct"], int(bool(cond["isp"])),
                           usuario, db.now_iso()))
        cid = cur.lastrowid
        for l in ls:
            ant = _dec(previas[l["id"]]["cant_origen"]) if l["id"] in previas else Decimal(0)
            imp_ant = previas[l["id"]]["importe_origen_cents"] if l["id"] in previas else 0
            con.execute("""INSERT INTO cert_prov_lineas (cert_id, linea_id, cant_anterior, cant_origen, cant_mes, precio, importe_origen_cents,
                           importe_anterior_cents, importe_mes_cents) VALUES (?,?,?,?,?,?,?,?,0)""",
                        (cid, l["id"], str(ant), str(ant), "0", l["precio"], imp_ant, imp_ant))
        db.audit(con, usuario, "cert_proveedor_nueva", "cert_proveedor", cid, {"oferta": oferta_id, "numero": num, "periodo": periodo})
    recalcular(con, cid)
    return cid


def detalle(con, cid: int) -> list[dict]:
    out = db.rows(con, """SELECT cl.*, l.codigo, l.descripcion, l.unidad, l.cantidad AS cant_contrato, l.importe_cents AS contrato_cents,
                                 l.cert_partida FROM cert_prov_lineas cl JOIN contrato_lineas l ON l.id=cl.linea_id
                          WHERE cl.cert_id=? ORDER BY l.orden, l.id""", (cid,))
    for r in out:
        cc = _dec(r["cant_contrato"])
        r["pct_origen"] = float(_dec(r["cant_origen"]) / cc * 100) if cc else None
    return out


def medir(con, cid: int, mediciones: dict[int, object], usuario: str, a_origen: bool = True, observaciones: dict | None = None) -> list[str]:
    """Guarda la medición. `mediciones` = {linea_id (de contrato): cantidad}. Por defecto la cantidad es A ORIGEN;
    con a_origen=False es la del mes. Devuelve los avisos (excesos, descertificaciones)."""
    c = db.one(con, "SELECT * FROM certs_proveedor WHERE id=?", (cid,))
    if not c or c["estado"] != "borrador":
        raise ValueError("Solo se mide una certificación en medición (borrador).")
    filas = {r["linea_id"]: r for r in db.rows(con, "SELECT * FROM cert_prov_lineas WHERE cert_id=?", (cid,))}
    observaciones = observaciones or {}
    with db.tx(con):
        for lid, v in mediciones.items():
            lid = int(lid)
            if lid not in filas:
                continue
            try:
                q = _dec(v)
            except Exception:
                raise ValueError(f"Cantidad no válida: {v!r}")
            ant = _dec(filas[lid]["cant_anterior"])
            origen = q if a_origen else ant + q
            con.execute("UPDATE cert_prov_lineas SET cant_origen=?, cant_mes=?, observaciones=COALESCE(?, observaciones) WHERE id=?",
                        (str(origen), str(origen - ant), observaciones.get(lid), filas[lid]["id"]))
        con.execute("UPDATE certs_proveedor SET medido_por=?, medido_en=? WHERE id=?", (usuario, db.now_iso(), cid))
        db.audit(con, usuario, "cert_proveedor_medicion", "cert_proveedor", cid, {"lineas": len(mediciones)})
    recalcular(con, cid)
    return avisos(con, cid)


def recalcular(con, cid: int) -> None:
    c = db.one(con, "SELECT * FROM certs_proveedor WHERE id=?", (cid,))
    orig = ant = 0
    for r in db.rows(con, "SELECT * FROM cert_prov_lineas WHERE cert_id=?", (cid,)):
        io_ = to_cents(_dec(r["cant_origen"]) * _dec(r["precio"]))
        ia = r["importe_anterior_cents"] if r["importe_anterior_cents"] is not None else to_cents(_dec(r["cant_anterior"]) * _dec(r["precio"]))
        con.execute("UPDATE cert_prov_lineas SET importe_origen_cents=?, importe_mes_cents=? WHERE id=?", (io_, io_ - ia, r["id"]))
        orig += io_
        ant += ia
    base = Decimal(orig - ant) / 100
    ret = q2(base * _dec(c["ret_pct"]) / 100) if base > 0 else Decimal(0)
    iva = Decimal(0) if c["isp"] else q2(base * _dec(c["iva_pct"]) / 100)
    total = base + iva
    con.execute("""UPDATE certs_proveedor SET base_origen_cents=?, base_anterior_cents=?, base_mes_cents=?, ret_cents=?, iva_cents=?,
                   total_cents=?, liquido_cents=? WHERE id=?""",
                (orig, ant, orig - ant, to_cents(ret), to_cents(iva), to_cents(total), to_cents(total - ret), cid))
    con.commit()


def avisos(con, cid: int) -> list[str]:
    out = []
    c = db.one(con, "SELECT * FROM certs_proveedor WHERE id=?", (cid,))
    for r in detalle(con, cid):
        o, a, cc = _dec(r["cant_origen"]), _dec(r["cant_anterior"]), _dec(r["cant_contrato"])
        nombre = f"{r['codigo'] or ''} {r['descripcion'][:50]}".strip()
        if cc and o > cc:
            out.append(f"EXCESO en «{nombre}»: a origen {o} {r['unidad'] or ''} frente a {cc} contratados "
                       f"({fmt_eur(Decimal(to_cents((o - cc) * _dec(r['precio']))) / 100)} de más).")
        if o < a:
            out.append(f"Se DESCERTIFICA «{nombre}»: a origen {o} frente a {a} ya certificados.")
        if o < 0:
            out.append(f"Cantidad negativa en «{nombre}».")
    if c and c["base_mes_cents"] == 0:
        out.append("La certificación del mes es 0 €.")
    return out


def hay_exceso(con, cid: int) -> bool:
    return any(a.startswith("EXCESO") for a in avisos(con, cid))


def aprobar(con, cid: int, usuario: str, rol: str, exceso_referencia: str = "") -> int:
    """Aprueba y genera el PDF de autorización de facturación + el correo al subcontratista. Devuelve el id del correo (o 0)."""
    c = db.one(con, "SELECT * FROM certs_proveedor WHERE id=?", (cid,))
    if not c or c["estado"] != "borrador":
        raise ValueError("Solo se aprueba una certificación en medición.")
    if rol not in ("admin", "direccion", "gestor"):
        raise ValueError("Aprueba Administración o Dirección.")
    if rol != "admin" and c["medido_por"] == usuario and db.get_setting(con, "cp_cuatro_ojos", "1") == "1":
        raise ValueError("Cuatro ojos: la certificación la aprueba una persona distinta de quien la midió.")
    if any(_dec(r["cant_origen"]) < 0 for r in detalle(con, cid)):
        raise ValueError("Hay cantidades negativas.")
    if hay_exceso(con, cid) and not exceso_referencia.strip():
        raise ValueError("Hay excesos sobre lo contratado: indique la orden de cambio aprobada que los cubre.")
    from . import cierres
    if cierres.cerrado(con, c["obra_id"], (c["fecha"] or c["periodo"] + "-01")):
        raise ValueError("El mes de esta certificación está cerrado.")
    with db.tx(con):
        con.execute("""UPDATE certs_proveedor SET estado='aprobada', aprobado_por=?, aprobado_en=?, exceso_autorizado=?, exceso_referencia=?
                       WHERE id=?""", (usuario, db.now_iso(), int(bool(exceso_referencia.strip())), exceso_referencia.strip() or None, cid))
        db.audit(con, usuario, "cert_proveedor_aprobada", "cert_proveedor", cid,
                 {"base_mes": c["base_mes_cents"], "exceso_ref": exceso_referencia or None})
    pdf = generar_pdf(con, cid)
    ruta = carpeta() / f"CP_{cid:05d}_{c['periodo']}_n{c['numero']}.pdf"
    ruta.write_bytes(pdf)
    con.execute("UPDATE certs_proveedor SET pdf_path=? WHERE id=?", (str(ruta), cid))
    con.commit()
    casar_facturas(con, c["obra_id"])
    return preparar_correo(con, cid, usuario)


def anular(con, cid: int, usuario: str, rol: str, motivo: str) -> None:
    c = db.one(con, "SELECT * FROM certs_proveedor WHERE id=?", (cid,))
    if not motivo.strip():
        raise ValueError("Indique el motivo.")
    if c["estado"] == "facturada":
        raise ValueError("Ya tiene factura: anule antes la factura o emita una certificación rectificativa el mes siguiente.")
    if c["estado"] == "aprobada" and rol not in ("admin", "direccion", "gestor"):
        raise ValueError("Solo Administración o Dirección anulan una certificación aprobada.")
    posterior = db.one(con, "SELECT numero FROM certs_proveedor WHERE oferta_id=? AND numero>? AND estado<>'anulada'", (c["oferta_id"], c["numero"]))
    if posterior:
        raise ValueError(f"Existe la certificación nº {posterior['numero']} posterior: anúlela antes.")
    with db.tx(con):
        con.execute("UPDATE certs_proveedor SET estado='anulada', anulado_por=?, motivo_anulacion=? WHERE id=?", (usuario, motivo.strip(), cid))
        if c.get("correo_id"):
            con.execute("UPDATE correo_salida SET estado='cancelado', cancelado_por=? WHERE id=? AND estado IN ('pendiente_aprobacion','aprobado','error')",
                        (usuario, c["correo_id"]))
        db.audit(con, usuario, "cert_proveedor_anulada", "cert_proveedor", cid, {"motivo": motivo})


# ============================================================================ documento y correo
def generar_pdf(con, cid: int) -> bytes:
    from .pdf_simple import Documento
    from .config import EMPRESA_NOMBRE, EMPRESA_NIF
    c = db.one(con, """SELECT c.*, COALESCE(pr.nombre, o.proveedor_nombre) AS proveedor, pr.nif, ob.codigo AS obra_cod, ob.nombre AS obra_nombre,
                              o.alcance FROM certs_proveedor c JOIN ofertas o ON o.id=c.oferta_id JOIN obras ob ON ob.id=c.obra_id
                       LEFT JOIN proveedores pr ON pr.id=c.proveedor_id WHERE c.id=?""", (cid,))
    cond = condiciones(con, c["oferta_id"])
    E = lambda x: fmt_eur(Decimal(x or 0) / 100)  # noqa: E731
    d = Documento(titulo=f"Certificación nº {c['numero']} · {c['proveedor']} · obra {c['obra_cod']}")
    d.parrafo(f"CERTIFICACIÓN Nº {c['numero']} · AUTORIZACIÓN DE FACTURACIÓN", 14, negrita=True)
    d.espacio(4)
    d.parrafo(f"{EMPRESA_NOMBRE} (NIF {EMPRESA_NIF})")
    d.parrafo(f"Subcontratista: {c['proveedor']}" + (f" (NIF {c['nif']})" if c.get("nif") else ""))
    d.parrafo(f"Obra: {c['obra_cod']} · {c['obra_nombre']}    Periodo: {c['periodo']}    Fecha: {c['fecha'] or date.today().isoformat()}")
    if c.get("alcance"):
        d.parrafo(f"Contrato: {c['alcance']}")
    d.espacio(6)
    filas = []
    for r in detalle(con, cid):
        filas.append([r["codigo"] or "", r["descripcion"], r["unidad"] or "", f"{_dec(r['cant_contrato']):,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                      f"{_dec(r['cant_anterior']):.2f}".replace(".", ","), f"{_dec(r['cant_origen']):.2f}".replace(".", ","),
                      f"{_dec(r['cant_mes']):.2f}".replace(".", ","), f"{_dec(r['precio']):.2f}".replace(".", ","), E(r["importe_mes_cents"])])
    d.tabla(["Código", "Descripción", "Ud", "Contrato", "Anterior", "Origen", "Mes", "Precio", "Importe mes"], filas,
            [7, 30, 4, 8, 8, 8, 7, 7, 11], derecha={3, 4, 5, 6, 7, 8}, tam=7.5)
    d.espacio(10)
    tot = [["Certificado a origen", E(c["base_origen_cents"])], ["Certificado anterior", E(c["base_anterior_cents"])],
           ["BASE IMPONIBLE DEL MES", E(c["base_mes_cents"])]]
    if c["isp"]:
        tot.append(["IVA", "Inversión del sujeto pasivo (art. 84.Uno.2º.f LIVA)"])
    else:
        tot.append([f"IVA {c['iva_pct']} %", E(c["iva_cents"])])
    tot += [["TOTAL FACTURA", E(c["total_cents"])], [f"Retención de garantía {c['ret_pct']} %", "-" + E(c["ret_cents"])],
            ["LÍQUIDO A PERCIBIR", E(c["liquido_cents"])]]
    d.tabla(["Concepto", "Importe"], tot, [60, 40], derecha={1})
    d.espacio(12)
    d.parrafo("Se autoriza la emisión de la factura correspondiente EXCLUSIVAMENTE por la base imponible del mes indicada. "
              "La factura debe hacer referencia a esta certificación (nº y obra), ir a nombre de la sociedad indicada y, en su caso, "
              "aplicar la inversión del sujeto pasivo. Las facturas que no coincidan con lo certificado se devolverán.", 8.5)
    if cond.get("forma_pago"):
        d.parrafo(f"Forma de pago: {cond['forma_pago']}.", 8.5)
    if c.get("exceso_referencia"):
        d.parrafo(f"Incluye excesos sobre contrato amparados por: {c['exceso_referencia']}.", 8.5)
    d.espacio(18)
    d.parrafo(f"Medido por: {c['medido_por'] or ''}        Aprobado por: {c['aprobado_por'] or ''} ({(c['aprobado_en'] or '')[:10]})", 8.5)
    return d.bytes()


def preparar_correo(con, cid: int, usuario: str) -> int:
    from . import correo
    c = db.one(con, """SELECT c.*, COALESCE(pr.nombre, o.proveedor_nombre) AS proveedor, ob.codigo AS obra_cod, ob.nombre AS obra_nombre
                       FROM certs_proveedor c JOIN ofertas o ON o.id=c.oferta_id JOIN obras ob ON ob.id=c.obra_id
                       LEFT JOIN proveedores pr ON pr.id=c.proveedor_id WHERE c.id=?""", (cid,))
    para = condiciones(con, c["oferta_id"]).get("email") or correo.email_proveedor(con, c["proveedor_id"])
    if not para or not c.get("pdf_path"):
        return 0
    E = lambda x: fmt_eur(Decimal(x or 0) / 100)  # noqa: E731
    cuerpo = (f"Estimados señores de {c['proveedor']}:\n\nAdjuntamos la certificación nº {c['numero']} de la obra {c['obra_cod']} "
              f"{c['obra_nombre']}, periodo {c['periodo']}, aprobada por un importe de {E(c['base_mes_cents'])} de base imponible.\n\n"
              f"Les rogamos que emitan la factura por ese importe exacto, haciendo referencia a la certificación nº {c['numero']} y a la obra "
              f"{c['obra_cod']}, y la envíen al buzón de facturas.\n\nUn saludo.")
    mid = correo.preparar(con, "autorizacion_facturacion", para, f"Certificación nº {c['numero']} obra {c['obra_cod']} · {c['periodo']} · "
                          f"autorización de facturación", cuerpo, usuario, adjuntos=[c["pdf_path"]], origen="cert_proveedor", origen_id=cid,
                          obra_id=c["obra_id"])
    con.execute("UPDATE certs_proveedor SET correo_id=? WHERE id=?", (mid, cid))
    con.commit()
    return mid


# ============================================================================ casación con la factura
def casar_facturas(con, obra_id: int | None = None) -> int:
    """Certificaciones aprobadas sin factura ↔ facturas del mismo proveedor y obra con la misma base (±1 %, mín. 1 €)."""
    q = "SELECT * FROM certs_proveedor WHERE estado='aprobada' AND documento_factura_id IS NULL AND proveedor_id IS NOT NULL"
    p = []
    if obra_id:
        q += " AND obra_id=?"; p.append(obra_id)
    usadas = {r["documento_factura_id"] for r in db.rows(con, "SELECT documento_factura_id FROM certs_proveedor WHERE documento_factura_id IS NOT NULL")}
    n = 0
    for c in db.rows(con, q + " ORDER BY numero", p):
        base = Decimal(c["base_mes_cents"])
        tol = max(Decimal(100), abs(base) * TOL_CASE)
        cands = [f for f in db.rows(con, """SELECT id, base_imponible_cents, fecha, numero FROM documentos WHERE proveedor_id=? AND obra_id=?
                                            AND tipo_documento='factura' AND estado NOT IN ('rechazada','eliminado','duplicado','sin_procesar')
                                            ORDER BY fecha""", (c["proveedor_id"], c["obra_id"]))
                 if f["id"] not in usadas and abs(Decimal(f["base_imponible_cents"] or 0) - base) <= tol]
        if cands:
            f = cands[0]
            con.execute("UPDATE certs_proveedor SET estado='facturada', documento_factura_id=? WHERE id=?", (f["id"], c["id"]))
            usadas.add(f["id"])
            db.audit(con, "casación", "cert_proveedor_facturada", "cert_proveedor", c["id"], {"factura": f["numero"]})
            n += 1
    con.commit()
    return n


def aprobadas_sin_factura(con, obra_id: int | None = None) -> list[dict]:
    q = """SELECT c.*, COALESCE(pr.nombre, o.proveedor_nombre) AS proveedor FROM certs_proveedor c JOIN ofertas o ON o.id=c.oferta_id
           LEFT JOIN proveedores pr ON pr.id=c.proveedor_id WHERE c.estado='aprobada' AND c.documento_factura_id IS NULL AND c.base_mes_cents<>0"""
    p = []
    if obra_id:
        q += " AND c.obra_id=?"; p.append(obra_id)
    return db.rows(con, q, p)


def coste_certificado_periodo(con, obra_id: int, periodo: str) -> int:
    r = db.one(con, "SELECT COALESCE(SUM(base_mes_cents),0) c FROM certs_proveedor WHERE obra_id=? AND periodo=? AND estado IN ('aprobada','facturada')",
               (obra_id, periodo))
    return r["c"] if r else 0
