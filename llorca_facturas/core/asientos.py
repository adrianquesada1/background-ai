"""Propuesta de asientos contables de las facturas recibidas aprobadas (para importar en SIS o revisar en Excel).
Cada asiento cuadra (debe = haber). Cuentas por defecto del PGC, configurables en Configuración."""
from __future__ import annotations

from decimal import Decimal

from . import db


def cuentas(con) -> dict:
    return {"proveedor": db.get_setting(con, "cta_proveedores", "400"), "iva": db.get_setting(con, "cta_iva_soportado", "472"),
            "iva_isp": db.get_setting(con, "cta_iva_repercutido_isp", "477"), "irpf": db.get_setting(con, "cta_irpf", "4751"),
            "ret_garantia": db.get_setting(con, "cta_ret_garantia_prov", "4009")}


def proponer(con, desde: str | None = None, hasta: str | None = None, obra_id: int | None = None, solo_no_exportadas: bool = False) -> list[dict]:
    cta = cuentas(con)
    q = """SELECT d.*, COALESCE(pr.nombre, d.emisor_nombre) AS prov, o.codigo AS obra FROM documentos d
           LEFT JOIN proveedores pr ON pr.id=d.proveedor_id LEFT JOIN obras o ON o.id=d.obra_id
           WHERE d.estado='aprobada' AND d.tipo_documento IN ('factura','abono','anticipo')"""
    p = []
    if desde:
        q += " AND d.fecha>=?"; p.append(desde)
    if hasta:
        q += " AND d.fecha<=?"; p.append(hasta)
    if obra_id:
        q += " AND d.obra_id=?"; p.append(obra_id)
    filas = []
    for n, d in enumerate(db.rows(con, q + " ORDER BY d.fecha, d.id", p), 1):
        base = Decimal(d["base_imponible_cents"] or 0) / 100
        cuota = Decimal(db.one(con, "SELECT COALESCE(SUM(cuota_cents),0) c FROM impuestos WHERE documento_id=?", (d["id"],))["c"]) / 100
        irpf = Decimal(d["irpf_cents"] or 0) / 100
        retg = Decimal(d["ret_garantia_cents"] or 0) / 100
        isp = bool(d["inversion_sujeto_pasivo"])
        if isp and not cuota:
            tipo = Decimal(db.get_setting(con, "iva_isp_defecto", "21"))
            cuota = (base * tipo / 100).quantize(Decimal("0.01"))
        total_prov = base + (Decimal(0) if isp else cuota) - irpf - retg
        concepto = f"Fra. {d['numero']} {d['prov']}"[:60]
        cc = d.get("centro_coste") or d["obra"]
        ctx = {"asiento": n, "fecha": d["fecha"], "documento": d["numero"], "nif": d["emisor_nif"], "centro_coste": cc, "pdf": d["file_path"],
               "doc_id": d["id"]}
        filas.append({**ctx, "cuenta": d["cuenta_contable"] or "601", "concepto": concepto, "debe": base, "haber": Decimal(0)})
        if cuota:
            filas.append({**ctx, "cuenta": cta["iva"], "concepto": "IVA soportado" + (" (ISP)" if isp else ""), "debe": cuota, "haber": Decimal(0)})
            if isp:
                filas.append({**ctx, "cuenta": cta["iva_isp"], "concepto": "IVA repercutido (ISP)", "debe": Decimal(0), "haber": cuota})
        if irpf:
            filas.append({**ctx, "cuenta": cta["irpf"], "concepto": "Retención IRPF", "debe": Decimal(0), "haber": irpf})
        if retg:
            filas.append({**ctx, "cuenta": cta["ret_garantia"], "concepto": "Retención de garantía", "debe": Decimal(0), "haber": retg})
        filas.append({**ctx, "cuenta": cta["proveedor"], "concepto": concepto, "debe": Decimal(0), "haber": total_prov})
    return filas


def cuadran(filas: list[dict]) -> list[int]:
    """Asientos que no cuadran (debería ser lista vacía)."""
    from collections import defaultdict
    s = defaultdict(Decimal)
    for f in filas:
        s[f["asiento"]] += f["debe"] - f["haber"]
    return [a for a, v in s.items() if abs(v) > Decimal("0.01")]
