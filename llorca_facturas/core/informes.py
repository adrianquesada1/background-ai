"""
Informe mensual por destinatario (jefe de obra, Dirección, Finanzas).

Cada informe explica en texto, con las cifras exactas del sistema, qué ha pasado en el mes y qué requiere atención.
La explicación se construye con reglas (sin IA): cada frase sale de un dato verificable.
Se genera en HTML con estilo corporativo, listo para imprimir o guardar como PDF desde el navegador.
"""
from __future__ import annotations

import html
from datetime import date
from decimal import Decimal

from . import db, obra_control as oc, auditoria as A, analytics
from .money import fmt_eur, from_cents

DESTINATARIOS = {"jefe_obra": "Jefe de obra", "direccion": "Dirección", "finanzas": "Administración y Finanzas"}


def _e(v) -> str:
    return fmt_eur(A.r2(v))


def _p(v) -> str:
    return f"{A.r2(v):.1f} %".replace(".", ",")


def _tabla(cab: list[str], filas: list[list], alinear_der: set[int] | None = None) -> str:
    alinear_der = alinear_der or set()
    th = "".join(f"<th>{html.escape(c)}</th>" for c in cab)
    trs = "".join("<tr>" + "".join(f"<td class='{'num' if i in alinear_der else ''}'>{html.escape(str(v))}</td>"
                                   for i, v in enumerate(f)) + "</tr>" for f in filas)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>" if filas else "<p class='vacio'>Sin datos.</p>"


def datos(con, obra_id: int, cert_id: int) -> dict:
    obra = db.one(con, "SELECT * FROM obras WHERE id=?", (obra_id,))
    cert = db.one(con, "SELECT * FROM certificaciones WHERE id=?", (cert_id,))
    mes = A.calcular(con, obra_id, cert_id, None, False, "mes", True)
    ori = A.calcular(con, obra_id, cert_id, None, False, "origen", True)
    cob, desc_cob = A.cobertura_coste(con, obra_id, ori)
    prev = db.one(con, "SELECT * FROM certificaciones WHERE obra_id=? AND numero<? ORDER BY numero DESC LIMIT 1", (obra_id, cert["numero"]))
    mes_prev = A.calcular(con, obra_id, prev["id"], None, False, "mes", True) if prev else None
    caps = oc.rentabilidad(con, obra_id, cert_id, "mes")
    return {"obra": obra, "cert": cert, "mes": mes, "origen": ori, "prev": mes_prev, "caps": caps, "cobertura": cob,
            "desc_cobertura": desc_cob, "proy": A.proyeccion(ori)}


def narrativa(d: dict) -> list[str]:
    """Frases que explican el mes. Cada una sale de una cifra comprobable."""
    m, frases = d["mes"], []
    frases.append(f"En el periodo de la certificación nº {d['cert']['numero']} se han certificado {_e(m['ventas'])} y el coste "
                  f"imputado es de {_e(m['compras'] + m['rrhh'])}, lo que deja un resultado bruto de {_e(m['resultado_sis'])} "
                  f"({_p(m['pct_sis'])}) y un resultado prudente de {_e(m['resultado'])} ({_p(m['pct'])}).")
    if d["prev"]:
        dif = m["pct"] - d["prev"]["pct"]
        frases.append(f"Frente al mes anterior, el margen prudente {'mejora' if dif >= 0 else 'empeora'} {_p(abs(dif))} "
                      f"(de {_p(d['prev']['pct'])} a {_p(m['pct'])}).")
    if m["devengado"]:
        frases.append(f"Hay {_e(m['devengado'])} de coste ya ejecutado sin factura (certificaciones o albaranes de proveedor): "
                      "se ha incluido en el coste como provisión.")
    caps = d["caps"]
    if not caps.empty:
        malos = caps[(caps["certificado_m"] > 0) & (caps["coste_m"] > caps["certificado_m"])].sort_values("margen_m")
        for r in malos.head(3).itertuples():
            frases.append(f"El capítulo {r.codigo} «{r.capitulo.title()}» tiene más coste ({_e(oc.m2d(r.coste_m))}) que certificación "
                          f"({_e(oc.m2d(r.certificado_m))}) este mes: pierde {_e(oc.m2d(-r.margen_m))}.")
        sin_venta = caps[(caps["certificado_m"] == 0) & (caps["coste_m"] > 0)]
        if len(sin_venta):
            frases.append(f"{len(sin_venta)} capítulo(s) tienen coste pero nada certificado en el mes "
                          f"({_e(oc.m2d(sin_venta['coste_m'].sum()))}): obra ejecutada sin certificar o facturas mal imputadas.")
        desv = caps.dropna(subset=["desv_estudio_m"]) if "desv_estudio_m" in caps else caps.iloc[0:0]
        if len(desv):
            peor = desv.sort_values("desv_estudio_m", ascending=False).iloc[0]
            if peor["desv_estudio_m"] > 0:
                frases.append(f"Frente al presupuesto de Estudios, la mayor desviación está en {peor['codigo']} «{peor['capitulo'].title()}»: "
                              f"{_e(oc.m2d(peor['desv_estudio_m']))} por encima del coste esperado según su avance.")
    if d["cobertura"] < 0.6:
        frases.append(f"Aviso: {d['desc_cobertura']}. Las cifras a origen no son representativas hasta cargar el coste completo.")
    p = d["proy"]
    if d["cobertura"] >= 0.6:
        frases.append(f"La proyección a fin de obra es de {_e(p['proyeccion'])} ({_p(p['pct'])} sobre el presupuesto de venta).")
    return frases


def generar_html(con, obra_id: int, cert_id: int, destinatario: str, autor: str) -> str:
    d = datos(con, obra_id, cert_id)
    obra, cert, m = d["obra"], d["cert"], d["mes"]
    secciones = [("Resumen", "".join(f"<p>{html.escape(f)}</p>" for f in narrativa(d)))]
    kpis = [("Certificado del mes", _e(m["ventas"])), ("Coste del mes", _e(m["compras"] + m["rrhh"])),
            ("Resultado prudente", f"{_e(m['resultado'])} · {_p(m['pct'])}"), ("Certificado a origen", _e(d["origen"]["ventas"]))]
    caps = d["caps"]
    if destinatario in ("jefe_obra", "direccion") and not caps.empty:
        v = caps[(caps["certificado_m"] != 0) | (caps["coste_m"] != 0)].sort_values("margen_m")
        secciones.append(("Capítulos del mes (de peor a mejor margen)", _tabla(
            ["Cap.", "Capítulo", "Certificado", "Coste", "Margen", "Margen %"],
            [[r.codigo, r.capitulo.title()[:40], _e(oc.m2d(r.certificado_m)), _e(oc.m2d(r.coste_m)), _e(oc.m2d(r.margen_m)),
              _p(r.margen_pct) if r.margen_pct == r.margen_pct and r.margen_pct is not None else "—"] for r in v.head(15).itertuples()],
            {2, 3, 4, 5})))
    if destinatario == "jefe_obra":
        pend = db.rows(con, """SELECT emisor_nombre, numero, fecha, base_imponible_cents FROM documentos WHERE obra_id=? AND conformado_por IS NULL
                               AND estado IN ('pendiente_revision','revisada') AND tipo_documento IN ('factura','abono','anticipo')""", (obra_id,))
        secciones.append(("Facturas pendientes de su conformidad", _tabla(["Proveedor", "Nº", "Fecha", "Base"],
            [[x["emisor_nombre"], x["numero"], x["fecha"], _e(from_cents(x["base_imponible_cents"] or 0))] for x in pend], {3})))
        try:
            comp = db.rows(con, "SELECT descripcion, responsable, fecha_limite FROM obra_compromisos WHERE obra_id=? AND estado='abierto' "
                                "AND fecha_limite < ? ORDER BY fecha_limite", (obra_id, date.today().isoformat()))
            secciones.append(("Compromisos vencidos", _tabla(["Compromiso", "Responsable", "Fecha límite"],
                                                             [[c["descripcion"][:80], c["responsable"], c["fecha_limite"]] for c in comp])))
        except Exception:
            pass
    if destinatario == "direccion":
        p = d["proy"]
        secciones.append(("Proyección a fin de obra", _tabla(["Concepto", "Importe"], [
            ["Resultado prudente a origen", _e(p["resultado_hoy"])], ["Beneficio por indirectos", _e(p["bfo_indirectos"])],
            ["Desviaciones de coste previstas", _e(p["desviaciones"])], ["GG de la facturación pendiente", _e(p["gg_pendientes"])],
            ["Proyección", f"{_e(p['proyeccion'])} ({_p(p['pct'])})"]], {1})))
        umbral = int(db.get_setting(con, "umbral_direccion_cents", "1000000") or 1000000)
        pend = db.rows(con, """SELECT emisor_nombre, numero, base_imponible_cents FROM documentos WHERE obra_id=? AND estado IN
                               ('pendiente_revision','revisada') AND ABS(COALESCE(base_imponible_cents,0))>=?""", (obra_id, umbral))
        secciones.append(("Pendiente de aprobación por Dirección", _tabla(["Proveedor", "Nº", "Base"],
            [[x["emisor_nombre"], x["numero"], _e(from_cents(x["base_imponible_cents"]))] for x in pend], {2})))
    ri = m.get("internos")
    if ri and destinatario in ("direccion", "finanzas") and ri["n"]:
        secciones.append(("Costes y ventas internos del periodo", _tabla(["Concepto", "Importe"], [
            ["Mano de obra propia", _e(ri["mano_obra"])], ["Otros costes internos (indirectos, gastos generales…)", _e(ri["coste"])],
            ["Cargos a subcontratas (restan)", "−" + _e(ri["cargos"])], ["Ventas internas (sin certificar)", _e(ri["venta"])],
            ["Movimientos sin validar", str(ri["borradores"])]], {1})))
    if destinatario == "finanzas":
        v = analytics.vencimientos(con, obra_id=obra_id)
        filas = []
        if not v.empty:
            for tramo, g in v.groupby("tramo"):
                filas.append([tramo, len(g), _e(from_cents(int(g["pagar_c"].sum())))])
        secciones.append(("Pagos pendientes por vencimiento", _tabla(["Tramo", "Facturas", "Importe"], filas, {1, 2})))
        rt = analytics.retenciones(con, obra_id=obra_id)
        secciones.append(("Retenciones de garantía pendientes de devolver",
                          f"<p>{_e(from_cents(int(rt[rt['ret_garantia_devuelta'] == 0]['ret_c'].sum())) if not rt.empty else 0)} en "
                          f"{0 if rt.empty else len(rt)} factura(s).</p>"))
        secciones.append(("Provisión: coste ejecutado sin factura", _tabla(["Proveedor", "Tipo", "Nº", "Fecha", "Base"],
            [[x["proveedor"], x["tipo"], x["numero"], x["fecha"], _e(from_cents(x["base_c"]))] for x in m["devengado_docs"]], {4})))
        inc = db.rows(con, """SELECT i.codigo, COUNT(*) n FROM incidencias i JOIN documentos d ON d.id=i.documento_id WHERE d.obra_id=?
                              AND i.resuelta=0 AND i.codigo IN ('C12','C26','C27','C28','C29','C31','C33','C34','C11') GROUP BY i.codigo""",
                      (obra_id,))
        nombres = {"C12": "Posibles duplicados", "C26": "Albaranes facturados dos veces", "C27": "Precios fuera de lo habitual",
                   "C28": "Dudas de IVA / ISP", "C29": "Factura a nombre de otra sociedad", "C31": "Facturas fuera de su mes",
                   "C33": "Facturas en otra moneda", "C34": "Operaciones intracomunitarias", "C11": "Cambios de cuenta bancaria"}
        secciones.append(("Controles abiertos", _tabla(["Control", "Facturas"], [[nombres.get(x["codigo"], x["codigo"]), x["n"]] for x in inc], {1})))
        ctas = db.rows(con, """SELECT COALESCE(cuenta_contable,'sin cuenta') c, COUNT(*) n, SUM(base_imponible_cents) s FROM documentos
                               WHERE obra_id=? AND estado='aprobada' GROUP BY c ORDER BY s DESC""", (obra_id,))
        secciones.append(("Imputación contable de lo aprobado", _tabla(["Cuenta", "Facturas", "Base"],
                                                                      [[x["c"], x["n"], _e(from_cents(x["s"] or 0))] for x in ctas], {1, 2})))
    kp = "".join(f"<div class='kpi'><div class='l'>{html.escape(a)}</div><div class='v'>{html.escape(b)}</div></div>" for a, b in kpis)
    cuerpo = "".join(f"<h2>{html.escape(t)}</h2>{c}" for t, c in secciones)
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Informe {html.escape(obra['codigo'])}</title>
<style>
@page {{ size: A4; margin: 16mm; }}
body {{ font-family: 'Segoe UI', Arial, sans-serif; color:#2E2E2E; font-size: 11pt; }}
.cab {{ border-left: 6px solid #E1251B; padding-left: 12px; margin-bottom: 14px; }}
h1 {{ margin: 0; font-size: 19pt; }} h2 {{ font-size: 13pt; border-bottom: 1px solid #E3E1D8; padding-bottom: 3px; margin-top: 22px; }}
.sub {{ color:#77756E; font-size: 9.5pt; }}
.kpis {{ display:flex; gap:10px; margin: 12px 0; }} .kpi {{ flex:1; border:1px solid #E3E1D8; border-radius:6px; padding:8px 10px; }}
.kpi .l {{ color:#77756E; font-size: 8.5pt; }} .kpi .v {{ font-weight:600; font-size: 12pt; }}
table {{ border-collapse: collapse; width: 100%; font-size: 9.5pt; }} th {{ background:#2E2E2E; color:#fff; text-align:left; padding:5px; }}
td {{ border-bottom:1px solid #E3E1D8; padding:4px 5px; }} td.num {{ text-align:right; white-space:nowrap; }}
.vacio {{ color:#77756E; font-style: italic; }} .pie {{ margin-top: 26px; color:#77756E; font-size: 8.5pt; }}
</style></head><body>
<div class="cab"><h1>Informe mensual · {html.escape(DESTINATARIOS.get(destinatario, destinatario))}</h1>
<div class="sub">Obra {html.escape(obra['codigo'])} · {html.escape(obra['nombre'])} · Certificación nº {cert['numero']} ({cert['fecha']}) ·
generado el {date.today():%d/%m/%Y} por {html.escape(autor)}</div></div>
<div class="kpis">{kp}</div>{cuerpo}
<div class="pie">Llorca Group · Control económico de obra. Cifras calculadas por el sistema con aritmética exacta a partir de la
certificación, las facturas registradas y los datos cargados. Documento interno.</div></body></html>"""
