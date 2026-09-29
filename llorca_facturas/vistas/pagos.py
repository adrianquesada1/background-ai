"""Pagos a proveedores: remesas SEPA con controles antifraude y confirmación con el banco."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import streamlit as st

from core import db, pagos as PG
from core.money import fmt_eur
from vistas.comun import get_con, usuario, rol, selector_obra, eur_col


def render():
    con = get_con()
    PG.init(con)
    st.title("Pagos y remesas")
    st.caption("Solo facturas aprobadas. Un IBAN cambiado sin verificar o no válido bloquea el pago. La remesa se genera en formato SEPA "
               "(pain.001) para subirla al banco, y las facturas se marcan pagadas solo cuando se confirma la ejecución.")
    admin = rol() in ("admin", "gestor", "direccion")
    c1, c2 = st.columns(2)
    hasta = c1.date_input("Vencimiento hasta", date.today() + timedelta(days=7), format="DD/MM/YYYY", key="pag_hasta")
    with c2:
        obra_id = selector_obra("pag_obra")
    cands = PG.candidatas(con, hasta.isoformat(), obra_id)
    if not cands:
        st.info("No hay facturas aprobadas pendientes de pago hasta esa fecha.")
    else:
        df = pd.DataFrame(cands)
        df["pagar"] = df["bloqueos"].map(lambda b: not b)
        df["importe"] = df["total_a_pagar_cents"] / 100
        df["bloqueo"] = df["bloqueos"].map(lambda b: "; ".join(b))
        df["aviso"] = df.get("avisos", pd.Series([[]] * len(df))).map(lambda a: "; ".join(a or []))
        ed = st.data_editor(df[["pagar", "id", "fecha_vencimiento", "proveedor", "numero", "obra", "iban_n", "importe", "bloqueo", "aviso"]], hide_index=True,
                            width="stretch", key="pag_ed", disabled=["id", "fecha_vencimiento", "proveedor", "numero", "obra", "iban_n", "importe", "bloqueo", "aviso"],
                            column_config={"pagar": st.column_config.CheckboxColumn("Pagar"), "iban_n": "IBAN", "importe": eur_col("Líquido (€)"),
                                           "bloqueo": "Bloqueo (no se puede pagar)", "aviso": "Aviso (revisar antes de pagar)"})
        sel = [int(r["id"]) for r in ed.to_dict("records") if r["pagar"]]
        tot = sum(Decimal(c["total_a_pagar_cents"]) for c in cands if c["id"] in sel) / 100
        a, b = st.columns([2, 1])
        fe = a.date_input("Fecha de ejecución en el banco", date.today() + timedelta(days=1), format="DD/MM/YYYY", key="pag_fe")
        if admin and b.button(f"Generar remesa ({len(sel)} · {fmt_eur(tot)})", type="primary", disabled=not sel):
            try:
                rid, xml = PG.generar(con, sel, fe.isoformat(), usuario(), rol())
                st.session_state["_remesa_xml"] = (rid, xml)
                st.success(f"Remesa #{rid} generada. Descárguela y súbala al banco.")
            except ValueError as e:
                st.error(str(e))
        if st.session_state.get("_remesa_xml"):
            rid, xml = st.session_state["_remesa_xml"]
            st.download_button("Descargar fichero SEPA (XML)", xml, file_name=f"remesa_{rid}.xml", mime="application/xml")
    st.subheader("Remesas")
    rem = db.rows(con, "SELECT * FROM remesas ORDER BY id DESC LIMIT 50")
    if not rem:
        st.caption("Sin remesas.")
    for r in rem:
        with st.container(border=True):
            x, y, z = st.columns([4, 1, 1])
            x.markdown(f"**{r['referencia']}** · {r['num_pagos']} pago(s) · {fmt_eur(Decimal(r['total_cents']) / 100)} · ejecución {r['fecha_ejecucion']} · "
                       f"**{r['estado']}** · {r['creado_por']}")
            if r["estado"] == "generada" and admin:
                if y.button("Confirmar pagada", key=f"rc_{r['id']}", help="El banco ha ejecutado la remesa: marca las facturas como pagadas."):
                    n = PG.confirmar(con, r["id"], r["fecha_ejecucion"], usuario()); st.toast(f"{n} factura(s) marcadas como pagadas"); st.rerun()
                if z.button("Anular", key=f"ra_{r['id']}", help="No se ha enviado al banco: las facturas vuelven a estar disponibles."):
                    PG.anular(con, r["id"], usuario()); st.rerun()
