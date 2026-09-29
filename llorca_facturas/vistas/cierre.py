"""Cierre mensual de obra: lista de comprobación, foto del resultado y bloqueo del periodo."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import cierres as K
from vistas.comun import get_con, usuario, rol, selector_obra


def render():
    con = get_con()
    K.init(con)
    st.title("Cierre mensual")
    st.caption("Antes de llevar el resultado de la obra a la cuenta de resultados se revisa la lista. Al cerrar se congela la foto del "
               "mes y el periodo queda bloqueado: nadie puede cambiar facturas ni costes internos de ese mes hasta que se reabra.")
    obra_id = selector_obra("cie_obra", permitir_todas=False)
    if not obra_id:
        return
    mes_ant = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    periodo = st.text_input("Mes (AAAA-MM)", mes_ant, key="cie_mes")
    import re
    if not re.fullmatch(r"\d{4}-\d{2}", periodo or ""):
        st.error("Formato AAAA-MM.")
        return
    lc = K.lista_comprobacion(con, obra_id, periodo)
    for x in lc:
        icono = ":green[:material/check_circle:]" if x["ok"] else (":red[:material/error:]" if x["grave"] else ":orange[:material/warning:]")
        st.markdown(f"{icono} **{x['punto']}** — {x['detalle']}")
    ya = K.cerrado(con, obra_id, f"{periodo}-01")
    admin = rol() in ("admin", "direccion", "gestor")
    if ya:
        st.success(f"El mes {periodo} está CERRADO.")
        if rol() in ("admin", "direccion"):
            with st.popover("Reabrir el mes"):
                motivo = st.text_input("Motivo (obligatorio)", key="cie_mot")
                if st.button("Reabrir", type="primary"):
                    try:
                        K.reabrir(con, obra_id, periodo, usuario(), rol(), motivo); st.rerun()
                    except ValueError as e:
                        st.error(str(e))
    elif admin:
        graves = [x for x in lc if x["grave"] and not x["ok"]]
        coment = st.text_input("Comentario del cierre" + (" (obligatorio para cerrar con puntos pendientes)" if graves else ""), key="cie_com")
        forzar = st.checkbox("Cerrar aunque haya puntos pendientes o el mes no haya terminado", disabled=not graves and periodo < date.today().strftime("%Y-%m"))
        if st.button("Cerrar el mes", type="primary"):
            try:
                K.cerrar(con, obra_id, periodo, usuario(), rol(), coment, forzar)
                st.success("Mes cerrado y bloqueado."); st.rerun()
            except ValueError as e:
                st.error(str(e))
    hist = K.listado(con, obra_id)
    if hist:
        st.subheader("Cierres de la obra")
        filas = []
        for h in hist:
            foto = json.loads(h["foto"] or "{}")
            filas.append({"Mes": h["periodo"], "Estado": h["estado"], "Cerrado por": h["cerrado_por"], "Fecha": (h["cerrado_en"] or "")[:16],
                          "Venta mes": foto.get("mes", {}).get("ventas"), "Resultado mes": foto.get("mes", {}).get("resultado"),
                          "Resultado origen": foto.get("origen", {}).get("resultado"), "Reapertura": h["motivo_reapertura"] or ""})
        st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch")
