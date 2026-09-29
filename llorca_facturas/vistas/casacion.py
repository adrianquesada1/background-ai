"""Casación triple (factura ↔ certificación del proveedor ↔ contrato), excepciones explicadas y dinero colgado."""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
import streamlit as st

from core import casacion as C
from core.money import fmt_eur
from vistas.comun import get_con, selector_obra, ir_a_documento, puede_editar


def render():
    con = get_con()
    st.title("Casación y excepciones")
    st.caption("En las subcontratas, cada factura debe casar con su certificación y con el contrato. Lo que no casa no se fuerza: "
               "aparece aquí con el descuadre explicado para que decida una persona.")
    obra_id = selector_obra("cas_obra")
    ind = C.indicadores(con, obra_id)
    k = st.columns(3)
    k[0].metric("Facturas de subcontratas con contrato", ind["facturas_con_contrato"])
    k[1].metric("Casadas sin excepción", ind["casadas"])
    k[2].metric("% de casación", f"{ind['pct_casacion']:.0f} %" if ind["pct_casacion"] is not None else "—")
    exc = ind["excepciones"]
    st.subheader(f"Excepciones ({len(exc)})")
    if not exc:
        st.caption("Sin excepciones. Nota: la casación se aplica a proveedores con oferta adjudicada en Contratación.")
    for x in exc:
        with st.container(border=True):
            a, b = st.columns([5, 1])
            a.markdown(f"**{x['proveedor']}** · factura {x['numero']} ({x['fecha']}) · {fmt_eur(Decimal(x['base_c'] or 0) / 100)} · obra {x['obra']}")
            for m in x["motivos"]:
                a.markdown(f"- {m}")
            if puede_editar() and b.button("Abrir", key=f"cas_{x['id']}"):
                ir_a_documento(x["id"])
    st.subheader("Entregas a cuenta pendientes de descontar (dinero colgado)")
    dc = C.dinero_colgado(con, obra_id)
    if not dc:
        st.caption("No hay anticipos pendientes de descontar.")
    else:
        df = pd.DataFrame(dc)
        st.dataframe(df.assign(anticipado=df["anticipado_c"] / 100, descontado=df["descontado_c"].abs() / 100, pendiente=df["pendiente_c"] / 100)
                     [["obra", "proveedor", "anticipado", "descontado", "pendiente"]], hide_index=True, width="stretch")
