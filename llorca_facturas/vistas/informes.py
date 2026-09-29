"""Informes mensuales por destinatario (jefe de obra, Dirección, Finanzas) con explicación de las desviaciones."""
from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

from core import informes as INF, obra_control as oc
from vistas.comun import get_con, usuario, selector_obra, rol


def render():
    con = get_con()
    st.title("Informes mensuales")
    st.caption("Informe listo para enviar o imprimir, con las cifras del sistema y una explicación en texto de lo relevante del mes.")
    c1, c2, c3 = st.columns([2, 2, 2])
    with c1:
        obra_id = selector_obra("inf_obra", permitir_todas=False)
    if not obra_id:
        return
    certs = oc.certificaciones(con, obra_id)
    if not certs:
        st.info("La obra no tiene certificaciones: el informe mensual parte de la certificación del mes.")
        return
    lab = {c["id"]: f"nº {c['numero']} · {c['fecha']}" for c in certs}
    cid = c2.selectbox("Certificación", list(lab), index=len(lab) - 1, format_func=lambda i: lab[i], key="inf_cert")
    opciones = list(INF.DESTINATARIOS) if rol() in ("admin", "direccion", "gestor") else ["jefe_obra"]
    dest = c3.selectbox("Destinatario", opciones, format_func=INF.DESTINATARIOS.get, key="inf_dest")
    with st.spinner("Preparando el informe…"):
        html_ = INF.generar_html(con, obra_id, cid, dest, usuario())
    st.download_button("Descargar informe (HTML, se imprime a PDF desde el navegador)", html_,
                       file_name=f"informe_{dest}_{lab[cid].split(' ')[1]}.html", mime="text/html", type="primary")
    components.html(html_, height=900, scrolling=True)
