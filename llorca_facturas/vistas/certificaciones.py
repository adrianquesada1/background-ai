"""Certificaciones a cliente: importación verificada, explorador por capítulos y comparación mes a mes."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import certificacion as C, db, maestros, obra_control as oc
from core.money import fmt_eur
from vistas.comun import get_con, usuario, eur_col, selector_obra

ROJO, GRIS, OSCURO = "#E1251B", "#B5B5B5", "#2B2B2B"
TIPO_LBL = {"contrato": "Contrato", "opcional": "Opcionales", "modificacion": "Revisión / modificación",
            "orden_cambio": "Órdenes de cambio"}


def m(v):
    return float(oc.m2d(v))


@st.cache_data(show_spinner=False)
def _leer(data: bytes, nombre: str = "x.pdf"):
    if nombre.lower().endswith((".xlsx", ".xlsm", ".csv")):
        c, det, _ = C.leer_excel(data, nombre)
        return c, [a for a in C.verificar(c) if not a[1].startswith("Partida") or "irregular" in a[1]]
    c = C.leer_pdf(data)
    return c, C.verificar(c)


def _importar(con):
    st.subheader("Importar certificación")
    st.caption("PDF de SIS (ORIGEN · ANTERIOR · ACTUAL) o exportación a Excel/CSV (SIS, Presto u hoja propia). Se lee sin IA y se "
               "verifica con su propia aritmética: cada partida, cada capítulo y el total deben cuadrar. Si una hoja solo trae el "
               "importe a origen, el «anterior» se toma de la certificación previa de la obra.")
    f = st.file_uploader("Certificación (PDF, Excel o CSV)", type=["pdf", "xlsx", "xlsm", "csv"], key="cert_up")
    if not f:
        return
    with st.spinner("Leyendo y verificando la certificación…"):
        c, avisos = _leer(f.getvalue(), f.name)
    if not c.lineas:
        st.error("No se han reconocido partidas en el documento. Compruebe que es una certificación (PDF de SIS o tabla con "
                 "columnas de código, descripción e importe).")
        return
    if c.numero is None:
        c.numero = st.number_input("Número de certificación (no figura en el documento)", 1, 999, 1)
    if not c.fecha:
        c.fecha = st.date_input("Fecha de la certificación (no figura en el documento)", format="DD/MM/YYYY").isoformat()
    k = st.columns(5)
    k[0].metric("Certificación", f"nº {c.numero}")
    k[1].metric("Fecha", pd.to_datetime(c.fecha).strftime("%d/%m/%Y") if c.fecha else "—")
    k[2].metric("A origen", fmt_eur(c.totales[0]) if c.totales else "—")
    k[3].metric("Este mes", fmt_eur(c.totales[2]) if c.totales and len(c.totales) > 2 else "—")
    k[4].metric("Partidas / capítulos", f"{len(c.lineas)} / {sum(1 for x in c.capitulos if x.nivel == 1)}")
    st.caption(f"Obra: **{c.obra}** · Presupuesto {c.presupuesto} · Cliente: {c.cliente} · {c.paginas} páginas")
    if avisos:
        st.error(f"La lectura no cuadra en {len(avisos)} punto(s). Revísalos antes de importar:")
        st.dataframe(pd.DataFrame(avisos, columns=["Severidad", "Detalle"]), hide_index=True, width="stretch")
    else:
        st.success(" Verificación completa: todas las partidas (origen = anterior + actual), todos los capítulos y el total "
                   "del documento cuadran al milésimo con lo impreso.")
    obras = maestros.listar_obras(con, solo_activas=False)
    sug = oc.buscar_obra_para_cert(con, c)
    ids = [o["id"] for o in obras]
    obra_id = st.selectbox("Obra", ids, index=ids.index(sug) if sug in ids else 0,
                           format_func=lambda i: next(f"{o['codigo']} · {o['nombre']}" for o in obras if o["id"] == i),
                           help="Detectada por el nº de presupuesto (240664 → 664) o por el nombre.")
    from core.pdf_utils import sha256 as _sha
    mismo_archivo = db.one(con, "SELECT c.numero, o.codigo FROM certificaciones c JOIN obras o ON o.id=c.obra_id WHERE c.file_hash=?",
                           (_sha(f.getvalue()),))
    if mismo_archivo:
        st.error(f"Este mismo archivo ya está importado como certificación nº {mismo_archivo['numero']} de la obra "
                 f"{mismo_archivo['codigo']}.")
    if c.totales:
        from core.certificacion import M as _M
        igual = db.one(con, "SELECT numero FROM certificaciones WHERE obra_id=? AND total_origen_m=? AND numero<>?",
                       (obra_id, _M(c.totales[0]), c.numero))
        if igual:
            st.warning(f"El importe a origen coincide exactamente con la certificación nº {igual['numero']}: "
                       "¿es la misma certificación con otro número?")
    if c.obra and c.presupuesto:
        o_ = db.one(con, "SELECT codigo FROM obras WHERE id=?", (obra_id,))
        if o_ and not str(c.presupuesto).endswith(o_["codigo"]) and oc.buscar_obra_para_cert(con, c) not in (None, obra_id):
            st.warning("La certificación parece de otra obra (según su nº de presupuesto). Compruebe la obra seleccionada.")
    existe = db.one(con, "SELECT id FROM certificaciones WHERE obra_id=? AND numero=?", (obra_id, c.numero))
    reemplazar = st.checkbox("Reemplazar la ya importada", value=False) if existe else False
    if existe and not reemplazar:
        st.info(f"La certificación nº {c.numero} ya está importada en esta obra.")
    forzar = st.checkbox("Importar aunque haya descuadres", value=False) if avisos else True
    if st.button("Importar certificación", type="primary", disabled=(existe and not reemplazar) or not forzar or bool(mismo_archivo)):
        cid = oc.guardar_certificacion(con, c, f.getvalue() if f.name.lower().endswith(".pdf") else None, f.name, obra_id, usuario(),
                                       reemplazar=bool(existe))
        st.session_state["cert_sel"] = cid
        st.success("Certificación importada.")
        ya = db.one(con, "SELECT COUNT(*) n FROM partidas WHERE obra_id=? AND origen_estructura LIKE 'cert:%'", (obra_id,))["n"]
        if not ya:
            st.info("Siguiente paso recomendado: pestaña **Estructura de coste** → usar los capítulos de la certificación como "
                    "partidas de coste, para comparar venta y coste con la misma estructura.")


def render():
    con = get_con()
    st.title("Certificaciones")
    obra_id = selector_obra("cert_obra", permitir_todas=False)
    if not obra_id:
        return
    certs = oc.certificaciones(con, obra_id)
    tabs = st.tabs(["Resumen", "Explorar partidas", "Comparar certificaciones", "Estructura de coste", "Importar"])
    with tabs[4]:
        _importar(con)
    if not certs:
        with tabs[0]:
            st.info("Aún no hay certificaciones en esta obra. Impórtala en la pestaña **Importar**.")
        return
    ids = [c["id"] for c in certs]
    lab = {c["id"]: f"nº {c['numero']} · {pd.to_datetime(c['fecha']).strftime('%d/%m/%Y') if c['fecha'] else ''}" for c in certs}
    sel = st.session_state.get("cert_sel") if st.session_state.get("cert_sel") in ids else ids[-1]

    # ------------------------------------------------------------ resumen
    with tabs[0]:
        cid = st.selectbox("Certificación", ids, index=ids.index(sel), format_func=lambda i: lab[i], key="cert_res")
        c = next(x for x in certs if x["id"] == cid)
        caps = oc.cert_capitulos_df(con, cid)
        cap1 = caps[caps["nivel"] == 1].copy()
        k = st.columns(4)
        k[0].metric("Certificado a origen", fmt_eur(oc.m2d(c["total_origen_m"])))
        k[1].metric("Certificado este mes", fmt_eur(oc.m2d(c["total_actual_m"])))
        oc_m = cap1.loc[cap1["tipo"] == "orden_cambio", "origen_m"].sum()
        k[2].metric("Órdenes de cambio a origen", fmt_eur(oc.m2d(oc_m)),
                    f"{oc_m / c['total_origen_m'] * 100:.1f} % del total".replace(".", ",") if c["total_origen_m"] else None,
                    delta_color="off")
        mod_m = cap1.loc[cap1["tipo"].isin(["modificacion", "opcional"]), "origen_m"].sum()
        k[3].metric("Revisiones y opcionales", fmt_eur(oc.m2d(mod_m)))
        with st.popover("Eliminar esta certificación", icon=":material/delete:"):
            st.caption("Se guarda íntegra en «Papelera y deshacer» y se puede restaurar.")
            if st.button("Confirmar eliminación", key=f"delcert_{cid}", type="primary"):
                from core import historial as H
                H.init(con)
                H.eliminar_certificacion(con, cid, usuario())
                st.rerun()
        st.caption(("Lectura verificada" if c["verificada"] else "Importada con avisos") +
                   f" · {c['filename'] or ''} · importada por {c['importado_por']} el {c['importado_en']}")
        enc = oc.comprobar_encadenado(con, obra_id)
        if enc:
            with st.expander(f" {len(enc)} incoherencia(s) entre certificaciones consecutivas"):
                st.write("\n".join(f"- {e}" for e in enc[:200]))
        elif len(certs) > 1:
            st.caption(" Encadenado correcto: el «anterior» de cada partida coincide con el «origen» de la certificación previa.")

        cap1["lbl"] = cap1["codigo"] + " · " + cap1["nombre"].str.title()
        fig = go.Figure()
        fig.add_bar(y=cap1["lbl"], x=cap1["anterior_m"] / 1000, name="Anterior", orientation="h", marker_color=GRIS)
        fig.add_bar(y=cap1["lbl"], x=cap1["actual_m"] / 1000, name="Este mes", orientation="h", marker_color=ROJO)
        fig.update_layout(barmode="relative", height=max(380, 26 * len(cap1)), margin=dict(l=10, r=10, t=10, b=10),
                          yaxis=dict(autorange="reversed"), xaxis_title="€", legend=dict(orientation="h"), separators=",.")
        st.plotly_chart(fig, width="stretch")
        vis = cap1[["codigo", "nombre", "tipo", "origen_m", "anterior_m", "actual_m"]].copy()
        vis["tipo"] = vis["tipo"].map(TIPO_LBL)
        for col in ("origen_m", "anterior_m", "actual_m"):
            vis[col] = vis[col] / 1000
        st.dataframe(vis, hide_index=True, width="stretch", column_config={
            "codigo": "Cap.", "nombre": "Capítulo", "tipo": "Tipo", "origen_m": eur_col("A origen (€)"),
            "anterior_m": eur_col("Anterior (€)"), "actual_m": eur_col("Este mes (€)")})
        if len(certs) > 1:
            serie = pd.DataFrame(certs)
            fig = go.Figure()
            fig.add_bar(x=serie["numero"].astype(str), y=serie["total_actual_m"] / 1000, name="Certificado del mes", marker_color=ROJO)
            fig.add_scatter(x=serie["numero"].astype(str), y=serie["total_origen_m"] / 1000, name="A origen", yaxis="y2",
                            line=dict(color=OSCURO))
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10), title="Evolución de la certificación",
                              xaxis_title="Nº certificación", yaxis2=dict(overlaying="y", side="right"), separators=",.",
                              legend=dict(orientation="h"))
            st.plotly_chart(fig, width="stretch")

    # ------------------------------------------------------------ explorar
    with tabs[1]:
        cid = st.selectbox("Certificación", ids, index=ids.index(sel), format_func=lambda i: lab[i], key="cert_exp")
        lin = oc.cert_lineas_df(con, cid)
        caps = oc.cert_capitulos_df(con, cid)
        nombres = dict(zip(caps["codigo"], caps["nombre"]))
        c1, c2, c3 = st.columns([2, 2, 2])
        capsel = c1.multiselect("Capítulos", caps.loc[caps["nivel"] == 1, "codigo"].tolist(),
                                format_func=lambda x: f"{x} · {nombres.get(x, '')}")
        filtro = c2.selectbox("Mostrar", ["Todas", "Con movimiento este mes", "Descertificadas este mes (negativas)",
                                          "Sin completar (% < 100)", "Sin precio (medición)"])
        q = c3.text_input("Buscar", placeholder="código o texto…", key="cert_q")
        d = lin.copy()
        if capsel:
            d = d[d["capitulo"].isin(capsel)]
        if filtro == "Con movimiento este mes":
            d = d[d["actual_m"] != 0]
        elif filtro.startswith("Descertificadas"):
            d = d[d["actual_m"] < 0]
        elif filtro.startswith("Sin completar"):
            d = d[d["pct_origen"].astype(float) < 100]
        elif filtro.startswith("Sin precio"):
            d = d[d["precio"].astype(float) == 0]
        if q:
            d = d[d.apply(lambda r: q.lower() in f"{r['codigo']} {r['titulo']} {r['descripcion']}".lower(), axis=1)]
        d["subcap"] = d["subcapitulo"].map(nombres)
        for col in ("origen_m", "anterior_m", "actual_m", "presupuesto_m"):
            d[col] = d[col].astype("float") / 1000
        for col in ("pct_origen", "precio", "cant_origen", "cant_presupuesto"):
            d[col] = pd.to_numeric(d[col], errors="coerce")
        st.caption(f"{len(d)} partidas · a origen {fmt_eur(d['origen_m'].sum())} · este mes {fmt_eur(d['actual_m'].sum())}")
        st.dataframe(d[["capitulo", "subcap", "codigo", "unidad", "titulo", "pct_origen", "cant_origen", "cant_presupuesto",
                        "precio", "presupuesto_m", "origen_m", "anterior_m", "actual_m", "descripcion"]],
                     hide_index=True, width="stretch", height=520, column_config={
                         "capitulo": "Cap.", "subcap": "Subcapítulo", "codigo": "Código", "unidad": "Ud", "titulo": "Partida",
                         "pct_origen": st.column_config.ProgressColumn("% a origen", format="%.1f %%", min_value=0, max_value=100),
                         "cant_origen": st.column_config.NumberColumn("Cant. origen", format="localized"),
                         "cant_presupuesto": st.column_config.NumberColumn("Cant. prevista", format="localized",
                                                                           help="Cantidad origen ÷ % a origen"),
                         "precio": st.column_config.NumberColumn("Precio venta", format="localized"),
                         "presupuesto_m": eur_col("Importe previsto (€)"), "origen_m": eur_col("A origen (€)"),
                         "anterior_m": eur_col("Anterior (€)"), "actual_m": eur_col("Este mes (€)"),
                         "descripcion": st.column_config.TextColumn("Descripción", width="large")})

    # ------------------------------------------------------------ comparar
    with tabs[2]:
        if len(certs) < 2:
            st.info("Importa al menos dos certificaciones para ver qué cambia de un mes a otro: partidas nuevas, eliminadas, "
                    "cambios de precio, cambios de medición prevista y partidas descertificadas.")
        else:
            c1, c2 = st.columns(2)
            a = c1.selectbox("Desde", ids, index=len(ids) - 2, format_func=lambda i: lab[i], key="cmp_a")
            b = c2.selectbox("Hasta", ids, index=len(ids) - 1, format_func=lambda i: lab[i], key="cmp_b")
            if a == b:
                st.warning("Elige dos certificaciones distintas.")
            else:
                cmp = oc.comparar(con, a, b)
                if cmp.empty:
                    st.success("Sin cambios de precio ni de estructura.")
                else:
                    res = cmp.groupby("cambio").agg(n=("codigo", "size"), impacto=("impacto_m", "sum")).reset_index()
                    res["impacto"] = res["impacto"] / 1000
                    st.dataframe(res, hide_index=True, column_config={"impacto": eur_col("Impacto (€)")})
                    tipo = st.multiselect("Tipo de cambio", res["cambio"].tolist(), default=res["cambio"].tolist())
                    cmp = cmp[cmp["cambio"].isin(tipo)].copy()
                    cmp["impacto"] = cmp["impacto_m"] / 1000
                    st.dataframe(cmp.drop(columns=["impacto_m"]), hide_index=True, width="stretch",
                                 column_config={"impacto": eur_col("Impacto (€)")})

    # ------------------------------------------------------------ estructura
    with tabs[3]:
        st.markdown("La venta se certifica por **capítulos de la certificación**. Para medir margen de verdad, el coste debe "
                    "imputarse con la **misma estructura**. Este paso sustituye las partidas genéricas de la obra por los "
                    "capítulos de la certificación y reclasifica todas las líneas de factura (las asignadas a mano se conservan "
                    "si hay un capítulo equivalente).")
        actuales = maestros.partidas_de_obra(con, obra_id)
        ya = any((p.get("origen_estructura") or "").startswith("cert:") for p in actuales)
        st.caption(f"Estructura actual: {'capítulos de certificación' if ya else 'partidas genéricas'} "
                   f"({len(actuales)} partidas).")
        cid = st.selectbox("Tomar la estructura de", ids, index=len(ids) - 1, format_func=lambda i: lab[i], key="cert_estr")
        conf = st.checkbox("Entiendo que se reclasificarán las líneas de factura de esta obra")
        if st.button("Usar los capítulos de la certificación como partidas de coste", type="primary", disabled=not conf):
            with st.spinner("Reestructurando y revalidando…"):
                r = oc.adoptar_estructura(con, obra_id, cid, usuario())
            st.success(f"Hecho. Líneas asignadas: {r.get('asignadas', 0)} · a «Sin asignar»: {r.get('sin_asignar', 0)}. "
                       "Revisa las no asignadas en  Rentabilidad → Relacionar.")
