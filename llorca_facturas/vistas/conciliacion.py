"""Conciliación bancaria: extractos Norma 43 o Excel, casación con remesas, pagos y cobros."""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
import streamlit as st

from core import db, conciliacion as BC
from core.money import fmt_eur
from vistas.comun import get_con, usuario, rol


def render():
    con = get_con()
    st.title("Conciliación bancaria")
    st.caption("Suba el extracto del banco (Norma 43 / cuaderno 43, o Excel/CSV). Los cargos que coinciden con una remesa confirman la "
               "remesa y marcan pagadas sus facturas; los abonos que coinciden con una factura emitida registran el cobro. Lo inequívoco "
               "se aplica solo; lo demás se propone para que usted elija.")
    admin = rol() in ("admin", "gestor", "direccion")
    r = BC.resumen(con)
    k = st.columns(4)
    k[0].metric("Movimientos importados", r["movimientos"])
    k[1].metric("Pendientes", r["pendientes"], help=f"El más antiguo: {r['mas_antiguo']}" if r["mas_antiguo"] else None)
    k[2].metric("Conciliados", r["conciliados"])
    k[3].metric("Sin contrapartida", r["otros"])
    if admin:
        c = st.columns([3, 2])
        up = c[0].file_uploader("Extracto (Norma 43 .n43/.txt/.aeb, o Excel/CSV)", key="bc_up")
        iban = c[1].text_input("IBAN de la cuenta (si el Excel no lo trae)", key="bc_iban")
        if up is not None and st.button("Importar extracto", type="primary"):
            try:
                res = BC.importar(con, up.getvalue(), up.name, usuario(), iban.replace(" ", "").upper())
                (st.success if res["cuadra"] else st.warning)(
                    f"{res['formato']}: {res['movimientos']} movimiento(s), {res['nuevos']} nuevos ({res['repetidos']} ya estaban). "
                    f"{res['aplicados']} conciliado(s) automáticamente." + ("" if res["cuadra"] else " ATENCIÓN: el extracto no cuadra."))
                for a in res["avisos"]:
                    st.warning(a)
            except ValueError as e:
                st.error(str(e))
        if st.button("Aplicar las casaciones inequívocas"):
            n = BC.aplicar_automaticas(con, usuario())
            st.toast(f"{n} movimiento(s) conciliados."); st.rerun()
    t1, t2 = st.tabs(["Pendientes", "Conciliados"])
    with t1:
        pend = BC.pendientes(con)
        if not pend:
            st.success("No hay movimientos pendientes.")
        for m in pend[:150]:
            with st.container(border=True):
                a, b = st.columns([3, 2])
                imp = Decimal(m["importe_cents"]) / 100
                a.markdown(f"**{m['fecha']}** · :{'red' if imp < 0 else 'green'}[**{fmt_eur(imp)}**]  \n:gray[{m['concepto'] or ''} {m['referencia'] or ''}]")
                if not admin:
                    continue
                cands = m["candidatos"]
                if cands:
                    opc = list(range(len(cands)))
                    i = b.selectbox("Contrapartida", opc, key=f"bc_c_{m['id']}",
                                    format_func=lambda k: f"{cands[k]['texto']} · confianza {cands[k]['confianza']:.0%}")
                    if b.button("Conciliar", key=f"bc_ok_{m['id']}", type="primary"):
                        try:
                            st.toast(BC.conciliar(con, m["id"], cands[i], usuario())); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                else:
                    b.caption("Sin contrapartida en la aplicación.")
                mot = b.text_input("Sin contrapartida: motivo", key=f"bc_m_{m['id']}", placeholder="comisión, nómina, impuesto…")
                if mot and b.button("Marcar", key=f"bc_o_{m['id']}"):
                    BC.marcar_otro(con, m["id"], mot, usuario()); st.rerun()
    with t2:
        hechos = db.rows(con, "SELECT * FROM banco_movimientos WHERE estado<>'pendiente' ORDER BY fecha DESC, id DESC LIMIT 300")
        if hechos:
            df = pd.DataFrame(hechos)
            df["importe"] = df["importe_cents"] / 100
            df["tipo"] = df["tipo"].map(lambda t: BC.TIPOS.get(t, t))
            st.dataframe(df[["fecha", "importe", "concepto", "tipo", "motivo", "conciliado_por", "conciliado_en"]], hide_index=True, width="stretch",
                         column_config={"importe": st.column_config.NumberColumn("Importe (€)", format="localized"), "tipo": "Conciliado como",
                                        "motivo": "Motivo", "conciliado_por": "Por", "conciliado_en": "Cuándo"})
            if admin:
                sel = st.selectbox("Deshacer una conciliación", [None] + [h["id"] for h in hechos], key="bc_desh",
                                   format_func=lambda i: "—" if i is None else next(f"{h['fecha']} · {Decimal(h['importe_cents']) / 100} € · {h['concepto'] or ''}"[:90]
                                                                                     for h in hechos if h["id"] == i))
                if sel and st.button("Deshacer", help="Anula el cobro registrado o devuelve la remesa a «pendiente de confirmar»."):
                    BC.deshacer(con, sel, usuario()); st.rerun()
