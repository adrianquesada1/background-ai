"""Contratación: ofertas por capítulo, comparativa, adjudicación, opiniones y consumo de cada contrato."""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import db, maestros, obra_control as oc
from core.money import fmt_eur, parse_amount, to_cents, from_cents
from vistas.comun import get_con, usuario, eur_col, selector_obra

ROJO, VERDE, GRIS = "#E1251B", "#2E7D4F", "#B5B5B5"
EST_LBL = {"solicitada": "Solicitada", "recibida": "Recibida", "en_negociacion": "En negociación",
           "adjudicada": "Adjudicada ", "descartada": "Descartada"}


def render():
    con = get_con()
    st.title("Contratación por capítulo")
    st.caption("Quién ha ofertado cada capítulo, quién lo valoró, a quién se adjudicó, qué opinión merece y cuánto lleva facturado "
               "frente a lo contratado.")
    obra_id = selector_obra("con_obra", permitir_todas=False)
    if not obra_id:
        return
    partidas = [p for p in maestros.partidas_de_obra(con, obra_id) if p["codigo"] != "99"]
    lab = {p["id"]: f"{p['codigo']} · {p['descripcion'][:45]}" for p in partidas}
    of = oc.ofertas_df(con, obra_id)
    tabs = st.tabs(["Comparativa por capítulo", "Nueva oferta", "Todas las ofertas", "Consumo de contratos", "Proveedores valorados"])

    with tabs[1]:
        provs = db.rows(con, "SELECT id, nombre, nif FROM proveedores ORDER BY nombre")
        with st.form("oferta_nueva", clear_on_submit=True):
            c1, c2 = st.columns(2)
            pid = c1.selectbox("Capítulo", list(lab), format_func=lambda i: lab[i])
            pv = c2.selectbox("Proveedor existente", [None] + [p["id"] for p in provs],
                              format_func=lambda i: "— otro (escribir abajo) —" if i is None else
                              next(f"{p['nombre']} ({p['nif'] or 's/NIF'})" for p in provs if p["id"] == i))
            c3, c4, c5 = st.columns([2, 1, 1])
            nombre = c3.text_input("…o nombre del proveedor nuevo")
            importe = c4.text_input("Importe ofertado (€)", placeholder="155000,00")
            fecha = c5.date_input("Fecha", date.today(), format="DD/MM/YYYY")
            alcance = st.text_input("Alcance", placeholder="Suministro y colocación de solados interiores, sin rodapié…")
            c6, c7, c8, c9 = st.columns(4)
            estado = c6.selectbox("Estado", oc.ESTADOS_OFERTA, index=1, format_func=EST_LBL.get)
            valorado = c7.text_input("Valorada por", placeholder="Pilar Sirvent")
            responsable = c8.text_input("Responsable ejecución", placeholder="Jefe de obra")
            punt = c9.slider("Valoración", 1, 5, 3)
            c10, c11 = st.columns(2)
            plazo = c10.text_input("Plazo")
            condiciones = c11.text_input("Condiciones (pago, retención, penalizaciones…)")
            opinion = st.text_area("Opinión / observaciones", height=70)
            if st.form_submit_button("Guardar oferta", type="primary"):
                if not importe.strip() or not (pv or nombre.strip()):
                    st.error("Proveedor e importe son obligatorios.")
                else:
                    oc.guardar_oferta(con, {"obra_id": obra_id, "partida_id": pid, "proveedor_id": pv,
                                            "proveedor_nombre": nombre.strip() or None, "alcance": alcance,
                                            "importe_cents": to_cents(parse_amount(importe)), "fecha": fecha.isoformat(),
                                            "estado": estado, "valorado_por": valorado, "responsable": responsable,
                                            "puntuacion": punt, "opinion": opinion, "condiciones": condiciones, "plazo": plazo},
                                      usuario())
                    st.success("Oferta guardada."); st.rerun()

    if of.empty:
        for t in (tabs[0], tabs[2], tabs[3], tabs[4]):
            with t:
                st.info("Aún no hay ofertas. Regístralas en «Nueva oferta».")
        return

    with tabs[0]:
        con_of = [p for p in lab if p in set(of["partida_id"].dropna().astype(int))]
        pid = st.selectbox("Capítulo", con_of, format_func=lambda i: lab[i], key="cmp_pid")
        comp = oc.comparativa_partida(con, obra_id, pid)
        d = of[(of["partida_id"] == pid)].copy()
        cert = oc.ultima_cert(con, obra_id)
        venta = 0
        if cert:
            cod = next(p["codigo"] for p in partidas if p["id"] == pid)
            venta = db.one(con, "SELECT COALESCE(SUM(COALESCE(presupuesto_m, origen_m)),0) s FROM cert_lineas WHERE cert_id=? "
                                "AND capitulo=?", (cert["id"], cod))["s"]
        k = st.columns(4)
        k[0].metric("Ofertas válidas", comp.get("n", 0))
        k[1].metric("Más barata", fmt_eur(from_cents(comp.get("min_c", 0))))
        k[2].metric("Dispersión", f"{comp.get('dispersion_pct') or 0:.1f} %".replace(".", ","),
                    help="(máx − mín) / mín. Una dispersión alta indica alcances no homogéneos: revisar antes de comparar.")
        k[3].metric("Venta prevista del capítulo", fmt_eur(oc.m2d(venta)) if venta else "—")
        d["lbl"] = d["proveedor"].fillna("?")
        colores = [VERDE if s == "adjudicada" else (GRIS if s == "descartada" else ROJO) for s in d["estado"]]
        fig = go.Figure(go.Bar(x=d["importe_cents"] / 100, y=d["lbl"], orientation="h", marker_color=colores,
                               text=[EST_LBL[s] for s in d["estado"]], textposition="auto"))
        if venta:
            fig.add_vline(x=float(oc.m2d(venta)), line_dash="dash", annotation_text="Venta prevista")
        fig.update_layout(height=max(220, 50 * len(d)), margin=dict(l=10, r=10, t=10, b=10), xaxis_title="€", separators=",.")
        st.plotly_chart(fig, width="stretch")
        if venta and comp.get("min_c"):
            mg = oc.m2d(venta) - from_cents(comp["min_c"])
            st.caption(f"Con la oferta más barata el margen previsto del capítulo sería {fmt_eur(mg)} "
                       f"({mg / oc.m2d(venta) * 100:.1f} % de la venta prevista).".replace(".", ",", 1))
        for r in d.itertuples():
            with st.container(border=True):
                a, b, c = st.columns([3, 2, 2])
                a.markdown(f"**{r.proveedor}** · {EST_LBL[r.estado]}  \n{r.alcance or ''}")
                b.markdown(f"**{fmt_eur(from_cents(r.importe_cents))}**  \nValoración {int(r.puntuacion or 0)}/5")
                c.caption(f"Valoró: {r.valorado_por or '—'} · Responsable: {r.responsable or '—'}  \n{r.condiciones or ''} {r.plazo or ''}")
                if r.opinion:
                    st.caption(f" {r.opinion}")
                nuevo = st.selectbox("Cambiar estado", oc.ESTADOS_OFERTA, index=oc.ESTADOS_OFERTA.index(r.estado),
                                     format_func=EST_LBL.get, key=f"est_{r.id}", label_visibility="collapsed")
                if nuevo != r.estado:
                    with db.tx(con):
                        con.execute("UPDATE ofertas SET estado=? WHERE id=?", (nuevo, r.id))
                        db.audit(con, usuario(), "estado_oferta", "oferta", r.id, {"de": r.estado, "a": nuevo})
                    st.rerun()

    with tabs[2]:
        t = of.copy()
        t["importe"] = t["importe_cents"] / 100
        t["estado"] = t["estado"].map(EST_LBL)
        st.dataframe(t[["id", "partida_codigo", "partida", "proveedor", "estado", "importe", "fecha", "valorado_por", "responsable",
                        "puntuacion", "alcance", "condiciones", "plazo", "opinion"]], hide_index=True, width="stretch",
                     column_config={"importe": eur_col("Importe (€)"), "puntuacion": st.column_config.NumberColumn("Valoración", format="%d / 5")})
        borrar = st.multiselect("Eliminar ofertas", t["id"].tolist(), format_func=lambda i: f"#{i}")
        if borrar and st.button("Eliminar"):
            with db.tx(con):
                con.execute(f"DELETE FROM ofertas WHERE id IN ({','.join('?' * len(borrar))})", borrar)
                db.audit(con, usuario(), "borrar_ofertas", "obra", obra_id, {"ids": borrar})
            st.rerun()

    with tabs[3]:
        st.caption("Por cada contrato adjudicado: facturado por ese proveedor en la obra frente al importe contratado.")
        adj = of[of["estado"] == "adjudicada"].copy()
        if adj.empty:
            st.info("No hay ofertas adjudicadas.")
        else:
            fact = {r["proveedor_id"]: r["s"] for r in db.rows(con, """
                SELECT proveedor_id, SUM(base_imponible_cents) s FROM documentos WHERE obra_id=? AND estado<>'rechazada'
                AND tipo_documento IN ('factura','abono','anticipo') GROUP BY proveedor_id""", (obra_id,))}
            por_nombre = {maestros.strip_accents(r["nombre"]): r["id"] for r in db.rows(con, "SELECT id, nombre FROM proveedores")}
            adj["pid"] = adj.apply(lambda r: r["proveedor_id"] if pd.notna(r["proveedor_id"]) else
                                   por_nombre.get(maestros.strip_accents(r["proveedor"] or "")), axis=1)
            adj["facturado"] = adj["pid"].map(lambda p: (fact.get(int(p), 0) if pd.notna(p) else 0)) / 100
            adj["contratado"] = adj["importe_cents"] / 100
            adj["consumo_pct"] = (adj["facturado"] / adj["contratado"] * 100).round(1)
            adj["pendiente"] = adj["contratado"] - adj["facturado"]
            st.dataframe(adj[["partida_codigo", "proveedor", "contratado", "facturado", "consumo_pct", "pendiente", "responsable"]],
                         hide_index=True, width="stretch", column_config={
                             "contratado": eur_col("Contratado (€)"), "facturado": eur_col("Facturado (€)"),
                             "pendiente": eur_col("Pendiente (€)"),
                             "consumo_pct": st.column_config.ProgressColumn("Consumo", format="%.1f %%", min_value=0, max_value=120)})
            exc = adj[adj["facturado"] > adj["contratado"]]
            for r in exc.itertuples():
                st.error(f"{r.proveedor}: facturado {fmt_eur(r.facturado)} supera lo contratado {fmt_eur(r.contratado)}.")
            st.caption("El facturado es por proveedor en toda la obra; si un proveedor tiene varios contratos, se muestra el total.")

    with tabs[4]:
        g = of.groupby("proveedor").agg(ofertas=("id", "size"), adjudicadas=("estado", lambda s: int((s == "adjudicada").sum())),
                                        valoracion=("puntuacion", "mean"), importe_ofertado=("importe_cents", "sum")).reset_index()
        g["importe_ofertado"] = g["importe_ofertado"] / 100
        g["tasa_exito_pct"] = (g["adjudicadas"] / g["ofertas"] * 100).round(0)
        st.dataframe(g.sort_values("valoracion", ascending=False), hide_index=True, width="stretch", column_config={
            "valoracion": st.column_config.NumberColumn("Valoración media", format="%.1f / 5"),
            "importe_ofertado": eur_col("Ofertado (€)"),
            "tasa_exito_pct": st.column_config.NumberColumn("Adjudicación", format="%d %%")})
