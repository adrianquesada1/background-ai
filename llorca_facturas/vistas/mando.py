"""Cuadro de mando: los indicadores de cada agente (facturación, tesorería, estudios) en una sola vista."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import db, casacion, tesoreria, estudios
from core.money import fmt_eur
from vistas.comun import get_con


def render():
    con = get_con()
    tesoreria.init(con); estudios.init(con)
    st.title("Cuadro de mando")
    st.subheader("Facturación")
    vol = pd.DataFrame(db.rows(con, """SELECT substr(creado_en,1,7) mes, COUNT(*) n FROM documentos WHERE estado<>'eliminado'
                                       GROUP BY mes ORDER BY mes"""))
    apro = db.rows(con, """SELECT d.id, d.creado_en, d.aprobado_en,
                                  (SELECT COUNT(*) FROM auditoria a WHERE a.entidad='documento' AND a.entidad_id=d.id
                                     AND a.accion IN ('editar_cabecera','editar_lineas','editar_impuestos')) AS ediciones
                           FROM documentos d WHERE d.estado='aprobada' AND d.aprobado_en IS NOT NULL""")
    dias = [(pd.to_datetime(a["aprobado_en"]) - pd.to_datetime(a["creado_en"])).total_seconds() / 86400 for a in apro if a["creado_en"]]
    primera = sum(1 for a in apro if not a["ediciones"])
    cas = casacion.indicadores(con)
    k = st.columns(4)
    k[0].metric("Documentos procesados", int(vol["n"].sum()) if not vol.empty else 0)
    k[1].metric("% casación sin excepción", f"{cas['pct_casacion']:.0f} %" if cas["pct_casacion"] is not None else "—")
    k[2].metric("Tiempo medio de registro a aprobación", f"{sum(dias) / len(dias):.1f} días" if dias else "—")
    k[3].metric("Aprobadas a la primera (sin correcciones)", f"{100 * primera / len(apro):.0f} %" if apro else "—")
    if not vol.empty:
        fig = go.Figure(go.Bar(x=vol["mes"], y=vol["n"], marker_color="#2E2E2E"))
        fig.update_layout(height=220, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="documentos / mes")
        st.plotly_chart(fig, width="stretch")
    st.subheader("Tesorería")
    ti = tesoreria.indicadores(con)
    pend = sum(x["pendiente_c"] for x in tesoreria.aging(con)) / 100
    ret = sum(x["pendiente_ret_c"] for x in tesoreria.radar_retenciones(con)) / 100
    k = st.columns(4)
    k[0].metric("Demora media de cobro", f"{ti['demora_media']:.0f} días" if ti["demora_media"] is not None else "—")
    k[1].metric("Pendiente de cobro", fmt_eur(pend))
    k[2].metric("Retenciones del cliente pendientes", fmt_eur(ret))
    k[3].metric("Avales con vencimiento vigilado", f"{ti['pct_avales_vigilados']:.0f} %" if ti["pct_avales_vigilados"] is not None else "—")
    st.subheader("Estudios")
    ei = estudios.indicadores(con)
    k = st.columns(3)
    k[0].metric("Tiempo medio por estudio", f"{ei['dias_medios']:.0f} días" if ei["dias_medios"] is not None else "—")
    k[1].metric("Ofertas comparadas por compra", f"{ei['ofertas_por_compra']:.1f}" if ei["ofertas_por_compra"] is not None else "—")
    k[2].metric("Estudios registrados", ei["estudios"])
