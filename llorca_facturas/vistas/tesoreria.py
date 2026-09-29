"""Tesorería: facturas emitidas desde la certificación, cobros, aging, retenciones de garantía del cliente y previsión de caja."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import db, tesoreria as T, obra_control as oc
from core.money import parse_amount, fmt_eur
from vistas.comun import get_con, usuario, rol, selector_obra, eur_col


def _e(c):
    return fmt_eur(Decimal(c or 0) / 100)


def render():
    con = get_con()
    T.init(con)
    st.title("Tesorería")
    st.caption("La certificación firmada se convierte en factura (borrador → emitida). Cobros, aging, retenciones que retiene el "
               "cliente y previsión de caja. El sistema propone; Administración aprueba cada emisión y cada reclamación.")
    ind = T.indicadores(con)
    k = st.columns(3)
    k[0].metric("Demora media de cobro", f"{ind['demora_media']:.0f} días" if ind["demora_media"] is not None else "—")
    k[1].metric("Retenciones reclamadas", fmt_eur(ind["retenciones_reclamadas"]))
    k[2].metric("Avales con vencimiento vigilado", f"{ind['pct_avales_vigilados']:.0f} %" if ind["pct_avales_vigilados"] is not None else "—")
    obra_id = selector_obra("tes_obra")
    tabs = st.tabs(["Facturas emitidas", "Aging de cobros", "Retenciones del cliente", "Previsión de caja", "Alta histórica"])
    admin = rol() in ("admin", "gestor", "direccion")
    cfg = T.config(con)

    with tabs[0]:
        if obra_id and admin:
            certs = oc.certificaciones(con, obra_id)
            con_fac = {r["cert_id"] for r in db.rows(con, "SELECT cert_id FROM facturas_emitidas WHERE cert_id IS NOT NULL AND estado<>'anulada'")}
            libres = [c for c in certs if c["id"] not in con_fac]
            with st.expander(f"Facturar una certificación ({len(libres)} sin facturar)", expanded=bool(libres)):
                if libres:
                    with st.form("tes_nueva"):
                        c1, c2, c3, c4, c5 = st.columns(5)
                        cid = c1.selectbox("Certificación", [c["id"] for c in libres],
                                           format_func=lambda i: next(f"nº {c['numero']} · {c['fecha']} · {fmt_eur(Decimal(c['total_actual_m']) / 1000)}"
                                                                      for c in libres if c["id"] == i))
                        serie = c2.selectbox("Serie SII", cfg["series"])
                        iva = c3.text_input("IVA %", str(cfg["iva_pct"]))
                        ret = c4.text_input("Retención garantía %", str(cfg["ret_pct"]))
                        fecha = c5.date_input("Fecha", date.today(), format="DD/MM/YYYY")
                        isp = st.checkbox("Inversión del sujeto pasivo (sin IVA)")
                        if st.form_submit_button("Crear borrador", type="primary"):
                            try:
                                fid = T.desde_certificacion(con, cid, serie, parse_amount(iva), parse_amount(ret), fecha.isoformat(), usuario(), isp)
                                st.success(f"Borrador #{fid} creado. Revise el NIF del cliente y emítala."); st.rerun()
                            except Exception as e:  # noqa: BLE001
                                st.error(str(e))
                else:
                    st.caption("Todas las certificaciones de la obra están facturadas.")
        fs = T.listado(con, obra_id)
        if not fs:
            st.caption("Sin facturas emitidas.")
        else:
            df = pd.DataFrame(fs)
            vis = df.assign(base=df["base_cents"] / 100, total=df["total_cents"] / 100, liquido=df["liquido_cents"] / 100,
                            pendiente=df["pendiente_c"] / 100, retencion=df["retencion_cents"] / 100)
            st.dataframe(vis[["id", "codigo", "estado", "fecha", "vencimiento", "cliente", "cliente_nif", "base", "total", "retencion", "liquido",
                              "pendiente"]], hide_index=True, width="stretch",
                         column_config={c: eur_col(c.capitalize() + " (€)") for c in ("base", "total", "retencion", "liquido", "pendiente")})
            sel = st.selectbox("Factura", df["id"].tolist(), key="tes_sel",
                               format_func=lambda i: f"#{i} · {df.set_index('id').loc[i, 'codigo'] or 'borrador'} · {df.set_index('id').loc[i, 'cliente']}")
            f = T.estado_factura(con, sel)
            a, b, c, d = st.columns(4)
            if f["estado"] == "borrador" and admin:
                with a.popover("Datos del cliente y emitir"):
                    cli = st.text_input("Cliente", f["cliente"] or "", key=f"tes_cli_{sel}")
                    nif = st.text_input("NIF del cliente", f["cliente_nif"] or "", key=f"tes_nif_{sel}")
                    if st.button("Guardar y EMITIR", type="primary", key=f"tes_emit_{sel}"):
                        con.execute("UPDATE facturas_emitidas SET cliente=?, cliente_nif=? WHERE id=?", (cli, nif.strip().upper(), sel)); con.commit()
                        try:
                            st.success(f"Emitida: {T.emitir(con, sel, usuario(), rol())}"); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                if b.button("Anular borrador", key=f"tes_anu_{sel}"):
                    con.execute("UPDATE facturas_emitidas SET estado='anulada' WHERE id=?", (sel,))
                    db.audit(con, usuario(), "anular_borrador_factura", "factura_emitida", sel, None); con.commit(); st.rerun()
            if f["estado"] == "emitida" and admin:
                with a.popover("Registrar cobro"):
                    concepto = st.radio("Concepto", ["factura", "retencion"], format_func={"factura": "Factura", "retencion": "Retención de garantía"}.get,
                                        key=f"tes_con_{sel}", horizontal=True)
                    imp = st.text_input("Importe (€)", key=f"tes_imp_{sel}",
                                        placeholder=str(Decimal(f["pendiente_c" if concepto == "factura" else "pendiente_ret_c"]) / 100))
                    fch = st.date_input("Fecha", date.today(), key=f"tes_fc_{sel}", format="DD/MM/YYYY")
                    medio = st.selectbox("Medio", ["Transferencia", "Pagaré", "Confirming", "Cheque", "Otro"], key=f"tes_med_{sel}")
                    if st.button("Guardar cobro", type="primary", key=f"tes_gc_{sel}"):
                        try:
                            T.cobrar(con, sel, fch.isoformat(), parse_amount(imp or "0"), concepto, medio, usuario()); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
            with c.popover("Seguimiento del cobro"):
                acc = st.selectbox("Acción", T.ACCIONES, key=f"tes_acc_{sel}")
                cont = st.text_input("Con quién", key=f"tes_ct_{sel}")
                resp = st.text_area("Respuesta del cliente", key=f"tes_rsp_{sel}", height=80)
                prox = st.date_input("Próxima acción", value=None, key=f"tes_px_{sel}", format="DD/MM/YYYY")
                if st.button("Registrar", key=f"tes_seg_{sel}"):
                    T.seguimiento(con, sel, f["obra_id"], acc, cont, resp, prox.isoformat() if prox else None, usuario()); st.rerun()
            d.download_button("Ver / imprimir factura", T.factura_html(con, sel), file_name=f"factura_{f['codigo'] or 'borrador_' + str(sel)}.html",
                              mime="text/html", key=f"tes_html_{sel}")
            seg = db.rows(con, "SELECT fecha, usuario, accion, contacto, respuesta, proxima_fecha, sobre FROM seguimiento_cobros WHERE factura_id=? "
                               "ORDER BY id DESC", (sel,))
            if seg:
                st.markdown("**Seguimiento**")
                st.dataframe(pd.DataFrame(seg), hide_index=True, width="stretch")

    with tabs[1]:
        ag = T.aging(con, obra_id)
        if not ag:
            st.caption("Nada pendiente de cobro.")
        else:
            df = pd.DataFrame(ag)
            orden = ["No vencido", "1-30 días", "31-60 días", "61-90 días", "Más de 90 días"]
            g = df.groupby("tramo")["pendiente_c"].sum().reindex(orden).fillna(0) / 100
            fig = go.Figure(go.Bar(x=g.index, y=g.values, marker_color=["#B5B3AA", "#F2B134", "#E8812C", "#E1251B", "#8E1510"]))
            fig.update_layout(height=260, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="€", separators=",.")
            st.plotly_chart(fig, width="stretch")
            por = df.groupby(["cliente", "tramo"])["pendiente_c"].sum().unstack(fill_value=0).reindex(columns=orden, fill_value=0) / 100
            st.dataframe(por, width="stretch", column_config={c: eur_col(c) for c in orden})

    with tabs[2]:
        rr = T.radar_retenciones(con, obra_id)
        if not rr:
            st.caption("Sin retenciones pendientes de devolver.")
        else:
            df = pd.DataFrame(rr)
            st.dataframe(df.assign(pendiente=df["pendiente_ret_c"] / 100, ult=df["ultima_reclamacion"].map(lambda x: f"{x['fecha']} · {x['usuario']}" if x else ""))
                         [["codigo", "cliente", "fecha", "retencion_vence", "dias_para_vencer", "situacion", "pendiente", "ult"]],
                         hide_index=True, width="stretch", column_config={"pendiente": eur_col("Pendiente (€)"), "ult": "Última reclamación",
                                                                          "dias_para_vencer": "Días para vencer"})
            sel = st.selectbox("Retención", df["id"].tolist(), key="tes_ret_sel",
                               format_func=lambda i: f"{df.set_index('id').loc[i, 'codigo']} · {df.set_index('id').loc[i, 'situacion']}")
            texto = st.text_area("Borrador de reclamación (revíselo antes de enviarlo)", T.borrador_reclamacion(con, sel, (st.session_state.get('auth') or {}).get('nombre', '')),
                                 height=260, key=f"tes_rec_{sel}")
            c1, c2 = st.columns(2)
            c1.download_button("Descargar reclamación", texto, file_name=f"reclamacion_retencion_{sel}.txt")
            if admin and c2.button("Registrar que se ha reclamado", key=f"tes_reg_{sel}"):
                T.seguimiento(con, sel, None, "Reclamación de retención", "", "Enviada reclamación", None, usuario(), "retencion"); st.rerun()

    with tabs[3]:
        saldo = st.text_input("Saldo bancario de hoy (€)", "0", key="tes_saldo")
        pv = pd.DataFrame(T.prevision_caja(con, 12, parse_amount(saldo or "0")))
        fig = go.Figure()
        fig.add_bar(x=pv["semana"], y=pv["cobros"].map(float), name="Cobros", marker_color="#2E7D4F")
        fig.add_bar(x=pv["semana"], y=(-pv["pagos"]).map(float), name="Pagos", marker_color="#E1251B")
        fig.add_scatter(x=pv["semana"], y=pv["saldo"].map(float), name="Saldo previsto", line=dict(color="#2E2E2E", width=3))
        fig.update_layout(barmode="relative", height=360, margin=dict(l=10, r=10, t=10, b=10), separators=",.", legend=dict(orientation="h"))
        st.plotly_chart(fig, width="stretch")
        negativas = pv[pv["saldo"] < 0]
        if len(negativas):
            st.error(f"Saldo previsto negativo a partir de la semana del {negativas.iloc[0]['semana']}.")
        st.caption("Cobros: facturas emitidas pendientes por vencimiento. Pagos: facturas de proveedor aprobadas y no pagadas por vencimiento. "
                   "Lo vencido y pendiente se sitúa en la primera semana.")

    with tabs[4]:
        if not admin or not obra_id:
            st.caption("Elija una obra (Administración).")
        else:
            st.caption("Dé de alta facturas emitidas anteriores para el aging y el inventario de retenciones (cuentas 430/4308).")
            with st.form("tes_hist", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                cod = c1.text_input("Nº de factura *"); fch = c2.date_input("Fecha", format="DD/MM/YYYY"); cli = c3.text_input("Cliente")
                c4, c5, c6, c7 = st.columns(4)
                nif = c4.text_input("NIF cliente"); base = c5.text_input("Base (€) *"); iva = c6.text_input("IVA %", str(cfg["iva_pct"]))
                ret = c7.text_input("Retención %", str(cfg["ret_pct"]))
                c8, c9 = st.columns(2)
                cob = c8.text_input("Ya cobrado de la factura (€)", "0"); rv = c9.date_input("Vencimiento de la retención", value=None, format="DD/MM/YYYY")
                if st.form_submit_button("Guardar", type="primary"):
                    try:
                        if not cod.strip() or not base.strip():
                            raise ValueError("Nº y base son obligatorios.")
                        T.alta_historica(con, obra_id, cli, nif, cod.strip(), fch.isoformat(), parse_amount(base), parse_amount(iva),
                                         parse_amount(ret), parse_amount(cob or "0"), usuario(), rv.isoformat() if rv else None)
                        st.success("Guardada.")
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))
