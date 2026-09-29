"""
Analítica de coste por obra / partida / proveedor / mes.

Invariante contable que se comprueba SIEMPRE:
    Σ coste por partidas  ==  Σ bases imponibles de los documentos computables
Para garantizarlo, si en un documento Σ líneas ≠ base imponible, la diferencia
se imputa como línea sintética "Ajuste de cuadre" en la partida 99 (Sin asignar),
de modo que nunca se pierde ni se inventa un céntimo.

Todas las sumas se hacen en céntimos (enteros) -> exactas.
El coste de obra se mide por BASE IMPONIBLE (el IVA soportado es deducible y no
es coste; con inversión del sujeto pasivo es neutro).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from . import db
from .config import TIPOS_COMPUTABLES, DIAS_GARANTIA
from .money import from_cents


def _where(obra_id=None, desde=None, hasta=None, estados=None, proveedor_id=None, alias="d"):
    w = [f"{alias}.tipo_documento IN ({','.join('?' * len(TIPOS_COMPUTABLES))})"]
    p: list = list(TIPOS_COMPUTABLES)
    estados = estados or ["pendiente_revision", "revisada", "aprobada"]
    w.append(f"{alias}.estado IN ({','.join('?' * len(estados))})")
    p += list(estados)
    if obra_id:
        w.append(f"{alias}.obra_id=?"); p.append(obra_id)
    if proveedor_id:
        w.append(f"{alias}.proveedor_id=?"); p.append(proveedor_id)
    if desde:
        w.append(f"{alias}.fecha>=?"); p.append(str(desde))
    if hasta:
        w.append(f"{alias}.fecha<=?"); p.append(str(hasta))
    return " AND ".join(w), p


def documentos_df(con, **f) -> pd.DataFrame:
    w, p = _where(**f)
    sql = f"""
      SELECT d.id, d.filename, d.tipo_documento, d.estado, d.numero, d.fecha, d.fecha_vencimiento,
             d.obra_id, o.codigo AS obra_codigo, o.nombre AS obra_nombre,
             d.proveedor_id, COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, d.emisor_nif AS nif,
             COALESCE(d.base_imponible_cents,0) AS base_c, COALESCE(d.total_iva_cents,0) AS iva_c,
             COALESCE(d.total_factura_cents,0) AS total_c, COALESCE(d.irpf_cents,0) AS irpf_c,
             COALESCE(d.ret_garantia_cents,0) AS ret_c, COALESCE(d.total_a_pagar_cents,0) AS pagar_c,
             d.pagada, d.fecha_pago, d.ret_garantia_devuelta, d.inversion_sujeto_pasivo, d.concepto_general, d.cuenta_contable,
             (SELECT COUNT(*) FROM incidencias i WHERE i.documento_id=d.id AND i.resuelta=0 AND i.severidad IN ('critica','alta')) AS inc_graves
      FROM documentos d
      LEFT JOIN obras o ON o.id=d.obra_id
      LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
      WHERE {w} ORDER BY d.fecha, d.id"""
    return pd.DataFrame(db.rows(con, sql, p))


def lineas_coste_df(con, **f) -> pd.DataFrame:
    """Líneas de coste + líneas sintéticas de ajuste para que Σ partidas == Σ bases."""
    docs = documentos_df(con, **f)
    cols = ["documento_id", "obra_id", "fecha", "proveedor_id", "proveedor", "numero", "descripcion",
            "cantidad", "unidad", "precio_unitario", "importe_c", "partida_id", "partida_codigo",
            "partida", "tipo_linea", "es_extra", "sintetica"]
    if docs.empty:
        return pd.DataFrame(columns=cols)
    ids = docs["id"].tolist()
    lin = pd.DataFrame(db.rows(con, f"""
        SELECT l.documento_id, l.descripcion, l.cantidad, l.unidad, l.precio_unitario, l.importe_cents AS importe_c,
               l.partida_id, p.codigo AS partida_codigo, p.descripcion AS partida, l.tipo_linea, l.es_extra
        FROM lineas l LEFT JOIN partidas p ON p.id=l.partida_id
        WHERE l.documento_id IN ({','.join('?' * len(ids))})""", ids))
    if lin.empty:
        lin = pd.DataFrame(columns=["documento_id", "descripcion", "cantidad", "unidad", "precio_unitario",
                                    "importe_c", "partida_id", "partida_codigo", "partida", "tipo_linea", "es_extra"])
    lin["sintetica"] = False
    suma = lin.groupby("documento_id")["importe_c"].sum() if not lin.empty else pd.Series(dtype="int64")
    ajustes = []
    for _, d in docs.iterrows():
        diff = int(d["base_c"]) - int(suma.get(d["id"], 0))
        if diff != 0:
            sa = db.one(con, "SELECT id, codigo, descripcion FROM partidas WHERE obra_id=? AND codigo='99'", (d["obra_id"],)) \
                if d["obra_id"] else None
            ajustes.append({"documento_id": d["id"], "descripcion": "Ajuste de cuadre (base imponible − Σ líneas)",
                            "cantidad": None, "unidad": None, "precio_unitario": None, "importe_c": diff,
                            "partida_id": sa and sa["id"], "partida_codigo": "99", "partida": "Sin asignar",
                            "tipo_linea": "ajuste", "es_extra": 0, "sintetica": True})
    if ajustes:
        lin = pd.concat([lin, pd.DataFrame(ajustes)], ignore_index=True)
    lin = lin.merge(docs[["id", "obra_id", "fecha", "proveedor_id", "proveedor", "numero"]],
                    left_on="documento_id", right_on="id", how="left").drop(columns=["id"])
    lin["partida_codigo"] = lin["partida_codigo"].fillna("99")
    lin["partida"] = lin["partida"].fillna("Sin asignar")
    lin["importe_c"] = lin["importe_c"].astype("int64")
    return lin[cols]


def comprobar_invariante(con, **f) -> tuple[bool, int, int]:
    docs = documentos_df(con, **f)
    lin = lineas_coste_df(con, **f)
    a = int(docs["base_c"].sum()) if not docs.empty else 0
    b = int(lin["importe_c"].sum()) if not lin.empty else 0
    return a == b, a, b


def resumen(con, **f) -> dict:
    docs = documentos_df(con, **f)
    if docs.empty:
        return {"n_docs": 0}
    pend = docs[docs["pagada"] == 0]
    ret_pend = docs[docs["ret_garantia_devuelta"] == 0]
    aprob = docs[docs["estado"] == "aprobada"]
    return {
        "n_docs": len(docs),
        "n_proveedores": docs["proveedor_id"].nunique(),
        "base": from_cents(docs["base_c"].sum()),
        "iva": from_cents(docs["iva_c"].sum()),
        "total_factura": from_cents(docs["total_c"].sum()),
        "irpf": from_cents(docs["irpf_c"].sum()),
        "ret_garantia": from_cents(docs["ret_c"].sum()),
        "ret_garantia_pendiente": from_cents(ret_pend["ret_c"].sum()),
        "total_a_pagar": from_cents(docs["pagar_c"].sum()),
        "pendiente_pago": from_cents(pend["pagar_c"].sum()),
        "base_aprobada": from_cents(aprob["base_c"].sum()),
        "n_con_incidencias": int((docs["inc_graves"] > 0).sum()),
        "fecha_min": docs["fecha"].dropna().min() if docs["fecha"].notna().any() else None,
        "fecha_max": docs["fecha"].dropna().max() if docs["fecha"].notna().any() else None,
    }


def por_partida(con, obra_id: int, **f) -> pd.DataFrame:
    lin = lineas_coste_df(con, obra_id=obra_id, **f)
    part = pd.DataFrame(db.rows(con, "SELECT id AS partida_id, codigo, descripcion, presupuesto_coste_cents AS ppto_c "
                                     "FROM partidas WHERE obra_id=? ORDER BY codigo", (obra_id,)))
    if part.empty:
        return part
    if lin.empty:
        agg = pd.DataFrame(columns=["partida_codigo", "coste_c", "n_lineas", "n_proveedores", "extras_c"])
    else:
        lin["extra_c"] = lin["importe_c"].where(lin["es_extra"].fillna(0).astype(int) == 1, 0)
        agg = lin.groupby("partida_codigo").agg(coste_c=("importe_c", "sum"), n_lineas=("importe_c", "size"),
                                                n_proveedores=("proveedor_id", "nunique"),
                                                extras_c=("extra_c", "sum")).reset_index()
    out = part.merge(agg, left_on="codigo", right_on="partida_codigo", how="left").drop(columns=["partida_codigo"])
    for c in ("coste_c", "n_lineas", "n_proveedores", "extras_c"):
        out[c] = out[c].fillna(0).astype("int64")
    out["desviacion_c"] = out["coste_c"] - out["ppto_c"]
    out["consumido_pct"] = out.apply(lambda r: round(r["coste_c"] / r["ppto_c"] * 100, 1) if r["ppto_c"] else None, axis=1)
    return out


def por_proveedor(con, **f) -> pd.DataFrame:
    docs = documentos_df(con, **f)
    if docs.empty:
        return docs
    g = docs.groupby(["proveedor_id", "proveedor", "nif"], dropna=False).agg(
        n_docs=("id", "size"), base_c=("base_c", "sum"), ret_c=("ret_c", "sum"),
        pagar_c=("pagar_c", "sum"),
        pendiente_c=("pagar_c", lambda s: int(s[docs.loc[s.index, "pagada"] == 0].sum())),
        primera=("fecha", "min"), ultima=("fecha", "max")).reset_index()
    g = g.sort_values("base_c", ascending=False)
    total = g["base_c"].sum()
    g["peso_pct"] = (g["base_c"] / total * 100).round(2) if total else 0
    g["acumulado_pct"] = g["peso_pct"].cumsum().round(2)
    return g


def mensual(con, **f) -> pd.DataFrame:
    docs = documentos_df(con, **f)
    if docs.empty or docs["fecha"].isna().all():
        return pd.DataFrame(columns=["mes", "base_c", "acumulado_c"])
    docs = docs.dropna(subset=["fecha"]).copy()
    docs["mes"] = docs["fecha"].str[:7]
    g = docs.groupby("mes")["base_c"].sum().reset_index().sort_values("mes")
    g["acumulado_c"] = g["base_c"].cumsum()
    return g


def partida_proveedor(con, obra_id: int, **f) -> pd.DataFrame:
    lin = lineas_coste_df(con, obra_id=obra_id, **f)
    if lin.empty:
        return lin
    lin["partida_lbl"] = lin["partida_codigo"] + " · " + lin["partida"]
    return lin.pivot_table(index="partida_lbl", columns="proveedor", values="importe_c", aggfunc="sum", fill_value=0)


def retenciones(con, **f) -> pd.DataFrame:
    docs = documentos_df(con, **f)
    if docs.empty:
        return docs
    r = docs[docs["ret_c"] != 0].copy()
    if r.empty:
        return r

    def lib(fch):
        try:
            return (datetime.strptime(fch, "%Y-%m-%d").date() + timedelta(days=DIAS_GARANTIA)).isoformat()
        except Exception:
            return None
    r["liberacion_estimada"] = r["fecha"].apply(lib)
    return r[["id", "proveedor", "nif", "numero", "fecha", "base_c", "ret_c", "liberacion_estimada",
              "ret_garantia_devuelta", "obra_codigo"]]


def vencimientos(con, **f) -> pd.DataFrame:
    """Aging de pagos pendientes (por fecha de vencimiento; si no hay, fecha de factura)."""
    docs = documentos_df(con, **f)
    if docs.empty:
        return docs
    p = docs[docs["pagada"] == 0].copy()
    hoy = date.today()

    def tramo(r):
        f_ = r["fecha_vencimiento"] if isinstance(r["fecha_vencimiento"], str) and r["fecha_vencimiento"] else r["fecha"]
        if not isinstance(f_, str) or not f_:
            return "Sin fecha"
        d = (datetime.strptime(f_, "%Y-%m-%d").date() - hoy).days
        if d < 0:
            return "Vencido"
        if d <= 30:
            return "0-30 días"
        if d <= 60:
            return "31-60 días"
        if d <= 90:
            return "61-90 días"
        return "> 90 días"
    p["tramo"] = p.apply(tramo, axis=1)
    return p


def historico_precios(con, texto: str, obra_id=None, limite=200) -> pd.DataFrame:
    """Precios unitarios históricos de un concepto (base para el Agente de Estudios)."""
    q = f"%{texto.strip()}%"
    sql = """SELECT l.descripcion, l.cantidad, l.unidad, l.precio_unitario, l.descuento_pct, l.importe_cents AS importe_c,
                    d.fecha, d.numero, COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, o.codigo AS obra
             FROM lineas l JOIN documentos d ON d.id=l.documento_id
             LEFT JOIN proveedores pr ON pr.id=d.proveedor_id LEFT JOIN obras o ON o.id=d.obra_id
             WHERE l.descripcion LIKE ? AND d.estado<>'rechazada' AND l.precio_unitario IS NOT NULL"""
    p = [q]
    if obra_id:
        sql += " AND d.obra_id=?"; p.append(obra_id)
    sql += " ORDER BY d.fecha DESC LIMIT ?"; p.append(limite)
    return pd.DataFrame(db.rows(con, sql, p))


def c2e(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Convierte columnas en céntimos (int) a euros (float solo para mostrar/graficar)."""
    out = df.copy()
    for c in cols:
        if c in out:
            out[c.replace("_c", "")] = out[c].astype("int64") / 100
    return out
