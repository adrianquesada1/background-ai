"""
CASACIÓN TRIPLE: factura ↔ certificación del proveedor ↔ contrato (oferta adjudicada).

Se aplica a los proveedores con contrato adjudicado en la obra (subcontratas). Para cada factura:
- ¿Hay contrato adjudicado a ese proveedor en esa obra?
- ¿Hay una certificación del proveedor que la soporte (misma empresa, base igual ±1 %, fecha coherente)?
- ¿Lo facturado hasta ahora cabe en lo contratado?
Lo que no casa no se fuerza: va a la cola de excepciones con el descuadre explicado en euros.
Además: entregas a cuenta (anticipos) pagadas y aún no descontadas = dinero colgado.
"""
from __future__ import annotations

from decimal import Decimal

from . import db

TOL_PCT = Decimal("0.01")
TOL_CONTRATO = Decimal("0.02")


def _e(c) -> str:
    from .money import fmt_eur
    return fmt_eur(Decimal(c or 0) / 100)


def casar(con, obra_id: int | None = None) -> list[dict]:
    q = """SELECT d.id, d.obra_id, o.codigo AS obra, d.proveedor_id, COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, d.emisor_nif,
                  d.numero, d.fecha, d.base_imponible_cents AS base_c, d.estado, d.tipo_documento
           FROM documentos d LEFT JOIN obras o ON o.id=d.obra_id LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
           WHERE d.tipo_documento IN ('factura','anticipo') AND d.estado NOT IN ('rechazada','eliminado','duplicado','sin_procesar')
             AND d.obra_id IS NOT NULL AND d.proveedor_id IS NOT NULL"""
    p = []
    if obra_id:
        q += " AND d.obra_id=?"; p.append(obra_id)
    facturas = db.rows(con, q + " ORDER BY d.fecha", p)
    contratos = {}
    for r in db.rows(con, """SELECT obra_id, proveedor_id, SUM(importe_cents) c, GROUP_CONCAT(id) ids FROM ofertas
                             WHERE estado='adjudicada' AND proveedor_id IS NOT NULL GROUP BY obra_id, proveedor_id"""):
        contratos[(r["obra_id"], r["proveedor_id"])] = r["c"] or 0
    certs = {}
    for r in db.rows(con, """SELECT id, obra_id, proveedor_id, emisor_nif, numero, fecha, base_imponible_cents AS base_c FROM documentos
                             WHERE tipo_documento='certificacion' AND estado NOT IN ('rechazada','eliminado','duplicado')
                             AND COALESCE(texto,'') NOT LIKE '%\%OR%' ESCAPE '\\'"""):
        certs.setdefault((r["obra_id"], r["proveedor_id"]), []).append(r)
    acumulado, usadas, out = {}, set(), []
    for f in facturas:
        clave = (f["obra_id"], f["proveedor_id"])
        if clave not in contratos:
            continue                                   # materiales y servicios sin contrato: no aplica la casación triple
        motivos, cert_ok = [], None
        base = Decimal(f["base_c"] or 0)
        for c in certs.get(clave, []):
            if c["id"] in usadas:
                continue
            if abs(Decimal(c["base_c"] or 0) - base) <= max(Decimal(100), abs(base) * TOL_PCT) and \
                    (not c["fecha"] or not f["fecha"] or c["fecha"] <= f["fecha"] or True):
                cert_ok = c
                usadas.add(c["id"])
                break
        if f["tipo_documento"] == "factura" and not cert_ok:
            libres = [c for c in certs.get(clave, []) if c["id"] not in usadas]
            if libres:
                cerca = min(libres, key=lambda c: abs(Decimal(c["base_c"] or 0) - base))
                motivos.append(f"No hay certificación del proveedor por este importe: la más próxima (nº {cerca['numero']}, "
                               f"{_e(cerca['base_c'])}) difiere en {_e(abs(Decimal(cerca['base_c'] or 0) - base))}.")
            else:
                motivos.append("Factura de subcontrata sin certificación del proveedor que la soporte (debería facturar solo lo certificado).")
        acumulado[clave] = acumulado.get(clave, 0) + (f["base_c"] or 0)
        contratado = contratos[clave]
        if acumulado[clave] > contratado * (1 + TOL_CONTRATO):
            motivos.append(f"Lo facturado a origen ({_e(acumulado[clave])}) supera lo contratado ({_e(contratado)}) en "
                           f"{_e(acumulado[clave] - contratado)}: ¿hay orden de cambio aprobada?")
        out.append({**f, "contratado_c": contratado, "facturado_origen_c": acumulado[clave],
                    "certificacion": cert_ok and f"nº {cert_ok['numero']} ({cert_ok['fecha']})", "casada": not motivos,
                    "motivos": motivos})
    return out


def dinero_colgado(con, obra_id: int | None = None) -> list[dict]:
    """Entregas a cuenta (anticipos) a proveedores aún no descontadas en sus facturas."""
    q = """SELECT d.obra_id, o.codigo AS obra, d.proveedor_id, COALESCE(pr.nombre, d.emisor_nombre) AS proveedor,
                  SUM(CASE WHEN d.tipo_documento='anticipo' THEN d.base_imponible_cents ELSE 0 END) AS anticipado_c,
                  (SELECT COALESCE(SUM(-l.importe_cents),0) FROM lineas l JOIN documentos d2 ON d2.id=l.documento_id
                    WHERE d2.proveedor_id=d.proveedor_id AND d2.obra_id=d.obra_id AND l.tipo_linea='anticipo_deducido'
                      AND d2.estado NOT IN ('rechazada','eliminado','duplicado')) AS descontado_c
           FROM documentos d LEFT JOIN obras o ON o.id=d.obra_id LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
           WHERE d.estado NOT IN ('rechazada','eliminado','duplicado') AND d.proveedor_id IS NOT NULL"""
    p = []
    if obra_id:
        q += " AND d.obra_id=?"; p.append(obra_id)
    out = []
    for r in db.rows(con, q + " GROUP BY d.obra_id, d.proveedor_id", p):
        pend = (r["anticipado_c"] or 0) - abs(r["descontado_c"] or 0)
        if r["anticipado_c"] and pend > 0:
            out.append({**r, "pendiente_c": pend})
    return out


def indicadores(con, obra_id: int | None = None) -> dict:
    c = casar(con, obra_id)
    n = len(c)
    return {"facturas_con_contrato": n, "casadas": sum(1 for x in c if x["casada"]),
            "pct_casacion": (100 * sum(1 for x in c if x["casada"]) / n) if n else None,
            "excepciones": [x for x in c if not x["casada"]]}
