"""Estudios: medición (BC3/Excel) → corte por industrial → petición de ofertas → comparativa homogénea → archivo de coste."""
from __future__ import annotations

import urllib.parse
from decimal import Decimal

import pandas as pd
import streamlit as st

from core import db, estudios as E, maestros
from core.auditoria import OFICIOS
from core.money import fmt_eur
from vistas.comun import get_con, usuario, rol, eur_col


def render():
    con = get_con()
    E.init(con)
    st.title("Estudios")
    st.caption("El técnico decide y firma el precio. El sistema normaliza la medición, propone el corte por industrial, prepara la "
               "separata, registra a quién se pidió oferta y compara las ofertas sobre el mismo árbol de partidas, con el histórico propio.")
    editar = rol() in ("admin", "direccion", "tecnico", "gestor")
    ests = db.rows(con, "SELECT * FROM estudios ORDER BY id DESC")
    with st.expander("Nuevo estudio desde medición (BC3 o Excel)", expanded=not ests):
        if not editar:
            st.caption("Solo Estudios, Dirección o Administración.")
        else:
            c1, c2 = st.columns(2)
            nombre = c1.text_input("Nombre del estudio", key="est_nom")
            cliente = c2.text_input("Cliente / promotor", key="est_cli")
            f = st.file_uploader("Medición", type=["bc3", "xlsx", "xls", "csv"], key="est_up")
            if f and st.button("Crear estudio", type="primary"):
                try:
                    ps = E.leer_bc3(f.getvalue()) if f.name.lower().endswith(".bc3") else E.leer_excel_mediciones(f.getvalue(), f.name)
                    eid = E.crear_estudio(con, nombre or f.name, cliente, ps, f.name, usuario())
                    st.session_state["est_sel"] = eid
                    st.success(f"Estudio creado con {len(ps)} partidas y el corte por industrial propuesto."); st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))
    if not ests:
        return
    ind = E.indicadores(con)
    k = st.columns(3)
    k[0].metric("Tiempo medio por estudio", f"{ind['dias_medios']:.0f} días" if ind["dias_medios"] is not None else "—")
    k[1].metric("Ofertas comparadas por compra", f"{ind['ofertas_por_compra']:.1f}" if ind["ofertas_por_compra"] is not None else "—")
    k[2].metric("Estudios", ind["estudios"])
    eid = st.selectbox("Estudio", [e["id"] for e in ests], key="est_sel",
                       format_func=lambda i: next(f"#{e['id']} · {e['nombre']} · {e['estado']}" for e in ests if e["id"] == i))
    e = next(x for x in ests if x["id"] == eid)
    ps = pd.DataFrame(db.rows(con, "SELECT * FROM estudio_partidas WHERE estudio_id=? ORDER BY orden", (eid,)))
    tabs = st.tabs(["1 · Corte por industrial", "2 · Pedir ofertas", "3 · Recibir ofertas", "4 · Comparativa", "5 · Archivo de coste"])
    oficios_ok = [n for n, t, _ in OFICIOS if t == "DIRECTO"]

    with tabs[0]:
        resumen = ps.groupby(ps["oficio"].fillna("— sin asignar —")).size().rename("partidas").reset_index()
        st.dataframe(resumen, hide_index=True)
        ed = st.data_editor(ps[["id", "capitulo", "codigo", "unidad", "descripcion", "medicion", "oficio", "oficio_origen"]], hide_index=True,
                            width="stretch", key=f"est_ed_{eid}", disabled=["id", "capitulo", "codigo", "unidad", "descripcion", "medicion", "oficio_origen"]
                            if editar else True, column_config={"oficio": st.column_config.SelectboxColumn("Industrial", options=oficios_ok),
                                                                "descripcion": st.column_config.TextColumn(width="large"),
                                                                "oficio_origen": "Origen"})
        cambios = [(r["oficio"], int(r["id"])) for r, o in zip(ed.to_dict("records"), ps.to_dict("records")) if r["oficio"] != o["oficio"]]
        if editar and st.button(f"Confirmar corte ({len(cambios)} cambio(s))", type="primary"):
            with db.tx(con):
                for of, pid in cambios:
                    con.execute("UPDATE estudio_partidas SET oficio=?, oficio_origen='técnico' WHERE id=?", (of, pid))
                con.execute("UPDATE estudio_partidas SET oficio_origen='confirmado' WHERE estudio_id=? AND oficio_origen='propuesto'", (eid,))
                db.audit(con, usuario(), "confirmar_corte", "estudio", eid, {"cambios": len(cambios)})
            st.rerun()

    oficios = [o for o in ps["oficio"].dropna().unique().tolist()]
    with tabs[1]:
        if not oficios:
            st.caption("Asigne industriales en el paso 1.")
        else:
            of = st.selectbox("Industrial", oficios, key="est_of")
            st.download_button("Descargar separata (Excel para el industrial)", E.separata_excel(con, eid, of),
                               file_name=f"separata_{e['nombre']}_{of}.xlsx".replace(" ", "_"))
            with st.form("est_sol", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                emp = c1.text_input("Empresa"); mail = c2.text_input("Correo"); medio = c3.selectbox("Medio", ["Correo", "Teléfono", "Plataforma", "Otro"])
                if st.form_submit_button("Registrar petición de oferta"):
                    try:
                        E.registrar_solicitud(con, eid, of, emp, mail, medio, usuario()); st.success("Registrada.")
                    except ValueError as ex:
                        st.error(str(ex))
            sols = db.rows(con, "SELECT empresa, email, medio, enviado_en, usuario, estado FROM estudio_solicitudes WHERE estudio_id=? AND oficio=?", (eid, of))
            if sols:
                st.markdown("**A quién se ha pedido**")
                st.dataframe(pd.DataFrame(sols), hide_index=True, width="stretch")
                mails = ",".join(s["email"] for s in sols if s["email"])
                if mails:
                    asunto = urllib.parse.quote(f"Petición de oferta · {e['nombre']} · {of}")
                    cuerpo = urllib.parse.quote(f"Buenos días,\n\nLes adjuntamos la separata de {of} del estudio «{e['nombre']}» para que nos "
                                                "remitan su mejor oferta rellenando la columna de precio unitario e indicando plazo, forma de "
                                                "pago, validez y exclusiones.\n\nGracias.\n")
                    st.link_button("Abrir correo a estos industriales (adjunte la separata)", f"mailto:?bcc={mails}&subject={asunto}&body={cuerpo}")

    with tabs[2]:
        sols = db.rows(con, "SELECT * FROM estudio_solicitudes WHERE estudio_id=? ORDER BY oficio, empresa", (eid,))
        if not sols:
            st.caption("Registre antes las peticiones de oferta.")
        else:
            sid = st.selectbox("Oferta de", [s["id"] for s in sols], key="est_sid",
                               format_func=lambda i: next(f"{s['oficio']} · {s['empresa']} · {s['estado']}" for s in sols if s["id"] == i))
            fo = st.file_uploader("Separata rellenada por el industrial (Excel)", type=["xlsx"], key=f"est_of_{sid}")
            if fo and editar and st.button("Leer oferta", type="primary"):
                r = E.importar_oferta(con, sid, fo.getvalue(), usuario())
                st.success(f"{r['precios']} precio(s) leídos.")
                if r["huecos"]:
                    st.warning(f"{len(r['huecos'])} partida(s) sin precio (huecos): " + ", ".join(r["huecos"][:20]))
                if r["desconocidos"]:
                    st.warning("Códigos que no están en la separata (el industrial la ha modificado): " + ", ".join(r["desconocidos"][:20]))

    with tabs[3]:
        if not oficios:
            st.caption("Sin industriales.")
        else:
            of = st.selectbox("Industrial", oficios, key="est_of_cmp")
            c = E.comparativa(con, eid, of)
            if not c["empresas"]:
                st.caption("Aún no hay ofertas recibidas para este industrial.")
            else:
                cols = st.columns(len(c["empresas"]))
                for col, s in zip(cols, c["empresas"]):
                    col.metric(s["empresa"], fmt_eur(c["totales"][s["id"]]), f"{c['huecos'][s['id']]} hueco(s)", delta_color="inverse")
                    col.caption(f"Plazo: {s['plazo'] or '—'} · Pago: {s['forma_pago'] or '—'} · Validez: {s['validez'] or '—'}"
                                + (f" · Excluye: {s['exclusiones']}" if s["exclusiones"] else ""))
                df = pd.DataFrame(c["partidas"])
                for s in c["empresas"]:
                    df[s["empresa"]] = df[s["empresa"]].map(lambda v: float(v) if v is not None else None)
                df["referencia"] = df["referencia"].map(lambda v: float(v) if v is not None else None)
                df["mejor"] = df["mejor"].map(lambda v: float(v) if v is not None else None)
                df["medicion"] = df["medicion"].map(float)
                st.dataframe(df, hide_index=True, width="stretch", column_config={"referencia": st.column_config.NumberColumn("Ref. histórica", format="%.2f"),
                                                                                "descripcion": st.column_config.TextColumn(width="large")})
                st.caption("Celdas vacías = huecos (la empresa no ha cotizado esa partida): no son comparables hasta cubrirlos.")

    with tabs[4]:
        data, res = E.archivo_coste(con, eid)
        k = st.columns(3)
        k[0].metric("Coste total estimado", fmt_eur(res["total"])); k[1].metric("Partidas", res["partidas"]); k[2].metric("Huecos sin precio", res["huecos"])
        st.download_button("Descargar archivo de coste (Excel)", data, file_name=f"archivo_coste_{e['nombre']}.xlsx".replace(" ", "_"), type="primary")
        st.caption("Orden de preferencia por partida: mejor oferta recibida → histórico propio → hueco. El precio de venta lo fija y firma Estudios.")
        if editar and e["estado"] == "abierto" and st.button("Cerrar el estudio"):
            con.execute("UPDATE estudios SET estado='cerrado', cerrado_por=?, cerrado_en=? WHERE id=?", (usuario(), db.now_iso(), eid))
            db.audit(con, usuario(), "cerrar_estudio", "estudio", eid, None); con.commit(); st.rerun()
