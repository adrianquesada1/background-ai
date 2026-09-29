"""Panel de control económico de obra: coste por partida, proveedor y mes."""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core import analytics, db, export
from core.money import fmt_eur, fmt_num, from_cents, pct
from vistas.comun import get_con, selector_obra, eur_col, cents_to_eur, ir_a_documento

ROJO, GRIS, OSCURO = "#E1251B", "#9A9A9A", "#2B2B2B"


def _filtros():
    c1, c2, c3, c4 = st.columns([2.2, 1, 1, 1.4])
    with c1:
        obra_id = selector_obra("panel_obra")
    with c2:
        desde = st.date_input("Desde", value=None, format="DD/MM/YYYY", key="panel_desde")
    with c3:
        hasta = st.date_input("Hasta", value=None, format="DD/MM/YYYY", key="panel_hasta")
    with c4:
        alcance = st.selectbox("Documentos", ["Todos salvo rechazados", "Solo aprobados"], key="panel_alc",
                               help="Las cifras de documentos no aprobados son provisionales.")
    f = {"obra_id": obra_id, "desde": desde, "hasta": hasta}
    if alcance == "Solo aprobados":
        f["estados"] = ["aprobada"]
    return {k: v for k, v in f.items() if v is not None}


def render():
    con = get_con()
    st.title("Panel de obra")
    f = _filtros()
    r = analytics.resumen(con, **f)
    if not r.get("n_docs"):
        st.info("No hay facturas para estos filtros. Empieza en ** Entrada de facturas**.")
        return

    obra = db.one(con, "SELECT * FROM obras WHERE id=?", (f["obra_id"],)) if f.get("obra_id") else None
    ppto = from_cents(obra["presupuesto_coste_cents"]) if obra else 0
    if obra and not ppto:
        ppto = from_cents(db.one(con, "SELECT COALESCE(SUM(presupuesto_coste_cents),0) s FROM partidas WHERE obra_id=?",
                                 (obra["id"],))["s"])

    k = st.columns(5)
    k[0].metric("Coste imputado (base imponible)", fmt_eur(r["base"]),
                help="Suma de bases imponibles de facturas, anticipos y abonos. El IVA no es coste.")
    if ppto:
        k[1].metric("Presupuesto de coste", fmt_eur(ppto), f"{fmt_num(pct(r['base'], ppto))} % consumido", delta_color="off")
    else:
        k[1].metric("Presupuesto de coste", "—", "sin presupuesto cargado", delta_color="off")
    k[2].metric("Retención de garantía retenida", fmt_eur(r["ret_garantia_pendiente"]),
                help="Importe retenido a industriales pendiente de devolver (pasivo con proveedores).")
    k[3].metric("Pendiente de pago", fmt_eur(r["pendiente_pago"]), help="Líquido a pagar de facturas no marcadas como pagadas.")
    k[4].metric("Documentos / con incidencias graves", f"{r['n_docs']} / {r['n_con_incidencias']}")

    ok, a, b = analytics.comprobar_invariante(con, **f)
    aprob = pct(r["base_aprobada"], r["base"])
    if ok:
        st.caption(f" Cuadre verificado: Σ partidas = Σ bases imponibles = {fmt_eur(from_cents(a))} · "
                   f"{fmt_num(aprob)} % del importe está aprobado · periodo {r['fecha_min'] or '—'} a {r['fecha_max'] or '—'}")
    else:
        st.error(f"Descuadre interno entre partidas y bases: {fmt_eur(from_cents(a - b))}. Revisa la auditoría.")

    tabs = st.tabs(["Partidas", "Proveedores", "Evolución mensual", "Partida × proveedor", "Extras",
                    "Retenciones de garantía", "Vencimientos", "Histórico de precios"])

    # ------------------------------------------------------------------ partidas
    with tabs[0]:
        if not f.get("obra_id"):
            st.info("Elige una obra para ver el coste por partida (cada obra tiene sus propias partidas).")
        else:
            pp = analytics.por_partida(con, f["obra_id"], **{k: v for k, v in f.items() if k != "obra_id"})
            vis = pp[(pp["coste_c"] != 0) | (pp["ppto_c"] != 0)].copy()
            vis["partida"] = vis["codigo"] + " · " + vis["descripcion"]
            fig = go.Figure()
            if vis["ppto_c"].sum():
                fig.add_bar(y=vis["partida"], x=vis["ppto_c"] / 100, name="Presupuesto", orientation="h", marker_color=GRIS)
            fig.add_bar(y=vis["partida"], x=vis["coste_c"] / 100, name="Coste real", orientation="h", marker_color=ROJO)
            fig.update_layout(barmode="group", height=max(260, 42 * len(vis)), margin=dict(l=10, r=10, t=10, b=10),
                              yaxis=dict(autorange="reversed"), xaxis_title="€", legend=dict(orientation="h"),
                              separators=",.")
            st.plotly_chart(fig, width="stretch")
            tabla = cents_to_eur(vis[["codigo", "descripcion", "ppto_c", "coste_c", "desviacion_c", "consumido_pct",
                                      "extras_c", "n_lineas", "n_proveedores"]],
                                 {"ppto_c": "Presupuesto", "coste_c": "Coste real", "desviacion_c": "Desviación", "extras_c": "Extras"})
            st.dataframe(tabla, hide_index=True, width="stretch", column_config={
                "codigo": "Cód.", "descripcion": "Partida",
                "Presupuesto": eur_col("Presupuesto (€)"), "Coste real": eur_col("Coste real (€)"),
                "Desviación": eur_col("Desviación (€)", "Coste real − presupuesto. Positivo = sobrecoste."),
                "consumido_pct": st.column_config.ProgressColumn("% consumido", format="%.1f %%", min_value=0, max_value=150),
                "Extras": eur_col("Extras (€)", "Líneas marcadas como extra / fuera de presupuesto."),
                "n_lineas": "Líneas", "n_proveedores": "Proveedores"})
            sel = st.selectbox("Ver detalle de partida", vis["codigo"].tolist(), key="panel_part_det",
                               format_func=lambda c: vis.set_index("codigo").loc[c, "partida"])
            lin = analytics.lineas_coste_df(con, **f)
            det = lin[lin["partida_codigo"] == sel]
            det_v = cents_to_eur(det[["documento_id", "fecha", "proveedor", "numero", "descripcion", "cantidad", "unidad",
                                      "precio_unitario", "importe_c", "tipo_linea"]], {"importe_c": "Importe"})
            st.dataframe(det_v, hide_index=True, width="stretch",
                         column_config={"Importe": eur_col("Importe (€)"), "documento_id": "Doc."})
            st.caption(f"Total partida: {fmt_eur(from_cents(det['importe_c'].sum()))} en {len(det)} líneas.")

    # ------------------------------------------------------------------ proveedores
    with tabs[1]:
        pv = analytics.por_proveedor(con, **f)
        fig = go.Figure()
        fig.add_bar(x=pv["proveedor"], y=pv["base_c"] / 100, name="Coste", marker_color=ROJO)
        fig.add_scatter(x=pv["proveedor"], y=pv["acumulado_pct"], name="% acumulado", yaxis="y2",
                        mode="lines+markers", line=dict(color=OSCURO))
        fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), separators=",.",
                          yaxis=dict(title="€"), yaxis2=dict(title="% acumulado", overlaying="y", side="right", range=[0, 105]),
                          legend=dict(orientation="h"))
        st.plotly_chart(fig, width="stretch")
        st.dataframe(cents_to_eur(pv.drop(columns=["proveedor_id"]),
                                  {"base_c": "Base", "ret_c": "Ret. garantía", "pagar_c": "A pagar", "pendiente_c": "Pendiente"}),
                     hide_index=True, width="stretch", column_config={
                         "Base": eur_col("Base (€)"), "Ret. garantía": eur_col("Ret. garantía (€)"),
                         "A pagar": eur_col("A pagar (€)"), "Pendiente": eur_col("Pendiente de pago (€)"),
                         "peso_pct": st.column_config.NumberColumn("% del coste", format="%.2f %%"),
                         "acumulado_pct": st.column_config.NumberColumn("% acumulado", format="%.2f %%"),
                         "n_docs": "Docs", "primera": "Primera", "ultima": "Última"})

    # ------------------------------------------------------------------ mensual
    with tabs[2]:
        m = analytics.mensual(con, **f)
        if m.empty:
            st.info("Sin fechas.")
        else:
            fig = go.Figure()
            fig.add_bar(x=m["mes"], y=m["base_c"] / 100, name="Coste del mes", marker_color=ROJO)
            fig.add_scatter(x=m["mes"], y=m["acumulado_c"] / 100, name="Acumulado", mode="lines+markers",
                            line=dict(color=OSCURO))
            if ppto:
                fig.add_hline(y=float(ppto), line_dash="dash", line_color=GRIS, annotation_text="Presupuesto de coste")
            fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="€", separators=",.",
                              legend=dict(orientation="h"))
            st.plotly_chart(fig, width="stretch")
            st.dataframe(cents_to_eur(m, {"base_c": "Coste del mes", "acumulado_c": "Acumulado"}), hide_index=True,
                         column_config={"Coste del mes": eur_col("Coste del mes (€)"), "Acumulado": eur_col("Acumulado (€)")})

    # ------------------------------------------------------------------ matriz
    with tabs[3]:
        if not f.get("obra_id"):
            st.info("Elige una obra.")
        else:
            mx = analytics.partida_proveedor(con, f["obra_id"], **{k: v for k, v in f.items() if k != "obra_id"})
            if mx.empty:
                st.info("Sin datos.")
            else:
                fig = px.imshow(mx / 100, text_auto=",.0f", aspect="auto", color_continuous_scale=["#FFFFFF", ROJO])
                fig.update_layout(height=max(300, 45 * len(mx)), margin=dict(l=10, r=10, t=10, b=10), separators=",.",
                                  coloraxis_colorbar_title="€")
                st.plotly_chart(fig, width="stretch")

    # ------------------------------------------------------------------ extras
    with tabs[4]:
        lin = analytics.lineas_coste_df(con, **f)
        ex = lin[lin["es_extra"].fillna(0).astype(int) == 1]
        st.caption("Líneas que el propio documento marca como extra o fuera de presupuesto: candidatas a orden de cambio "
                   "o a repercutir al cliente.")
        st.dataframe(cents_to_eur(ex[["documento_id", "fecha", "proveedor", "numero", "partida_codigo", "descripcion", "importe_c"]],
                                  {"importe_c": "Importe"}), hide_index=True, width="stretch",
                     column_config={"Importe": eur_col("Importe (€)")})
        st.metric("Total extras", fmt_eur(from_cents(ex["importe_c"].sum())))

    # ------------------------------------------------------------------ retenciones
    with tabs[5]:
        rt = analytics.retenciones(con, **f)
        if rt.empty:
            st.info("No hay retenciones de garantía en estos documentos.")
        else:
            g = rt.groupby("proveedor")["ret_c"].sum().sort_values(ascending=False).reset_index()
            c1, c2 = st.columns([1, 2])
            c1.dataframe(cents_to_eur(g, {"ret_c": "Retenido"}), hide_index=True,
                         column_config={"Retenido": eur_col("Retenido (€)")})
            c2.dataframe(cents_to_eur(rt, {"base_c": "Base", "ret_c": "Retención"}), hide_index=True, width="stretch",
                         column_config={"Base": eur_col("Base (€)"), "Retención": eur_col("Retención (€)"),
                                        "liberacion_estimada": "Liberación estimada",
                                        "ret_garantia_devuelta": st.column_config.CheckboxColumn("Devuelta")})

    # ------------------------------------------------------------------ vencimientos
    with tabs[6]:
        v = analytics.vencimientos(con, **f)
        if v.empty:
            st.success("No hay pagos pendientes.")
        else:
            orden = ["Vencido", "0-30 días", "31-60 días", "61-90 días", "> 90 días", "Sin fecha"]
            g = v.groupby("tramo")["pagar_c"].agg(["sum", "size"]).reindex(orden).dropna().reset_index()
            fig = px.bar(g, x="tramo", y=g["sum"] / 100, text=g["size"].astype(int).astype(str) + " fras.",
                         color_discrete_sequence=[ROJO])
            fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="€", xaxis_title="",
                              separators=",.")
            st.plotly_chart(fig, width="stretch")
            st.dataframe(cents_to_eur(v[["id", "proveedor", "numero", "fecha", "fecha_vencimiento", "tramo", "pagar_c"]],
                                      {"pagar_c": "A pagar"}), hide_index=True, width="stretch",
                         column_config={"A pagar": eur_col("A pagar (€)")})
            st.caption("Márcalas como pagadas en  Documentos y pagos.")

    # ------------------------------------------------------------------ precios
    with tabs[7]:
        st.caption("Histórico de precios unitarios pagados: base para comparar ofertas de Estudios con precios reales.")
        q = st.text_input("Material o trabajo", placeholder="p.ej. porcelánico, papel, inodoro, hormigón…", key="panel_hp")
        if q:
            hp = analytics.historico_precios(con, q, obra_id=f.get("obra_id"))
            if hp.empty:
                st.info("Sin coincidencias con precio unitario.")
            else:
                st.dataframe(cents_to_eur(hp, {"importe_c": "Importe"}), hide_index=True, width="stretch",
                             column_config={"Importe": eur_col("Importe (€)")})

    st.divider()
    if st.button("Preparar Excel con todo el análisis", icon=":material/download:"):
        st.session_state["panel_xlsx"] = export.exportar_excel(con, **f)
    if st.session_state.get("panel_xlsx"):
        st.download_button("Descargar Excel", st.session_state["panel_xlsx"],
                           file_name=f"analisis_obra_{date.today():%Y%m%d}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
