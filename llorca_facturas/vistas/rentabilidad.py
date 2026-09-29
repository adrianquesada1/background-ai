"""Rentabilidad de obra: lo certificado (venta) frente a lo facturado (coste) y lo contratado, capítulo a capítulo."""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import db, maestros, obra_control as oc
from core.money import fmt_eur, parse_amount, to_cents
from vistas.comun import get_con, usuario, eur_col, selector_obra, icono_sev

ROJO, VERDE, GRIS, OSCURO = "#E1251B", "#2E7D4F", "#B5B5B5", "#2B2B2B"


def e(m_):
    return fmt_eur(oc.m2d(m_))


def render():
    con = get_con()
    st.title("Rentabilidad de obra")
    obra_id = selector_obra("rent_obra", permitir_todas=False)
    if not obra_id:
        return
    certs = oc.certificaciones(con, obra_id)
    if not certs:
        st.info("Para calcular márgenes hace falta la venta: importa la certificación en ** Certificaciones**.")
        return
    lab = {c["id"]: f"nº {c['numero']} · {c['fecha']}" for c in certs}
    c1, c2, c3 = st.columns([2, 3, 2])
    cid = c1.selectbox("Certificación", list(lab), index=len(lab) - 1, format_func=lambda i: lab[i])
    cert = next(c for c in certs if c["id"] == cid)
    modo_lbl = c2.radio("Comparar", ["Mes de la certificación", "A origen", "Periodo personalizado"], horizontal=True,
                        help="Mes: certificado del mes frente a facturas de su periodo. A origen: todo lo certificado frente a todo "
                             "el coste cargado hasta la fecha de la certificación.")
    alcance = c3.selectbox("Facturas", ["Todas salvo rechazadas", "Solo aprobadas"])
    estados = ["aprobada"] if alcance == "Solo aprobadas" else None
    desde = hasta = None
    if modo_lbl == "Periodo personalizado":
        d1, d2 = st.columns(2)
        pd_, ph_ = oc.periodo_cert(con, cert)
        desde = d1.date_input("Coste desde", date.fromisoformat(pd_), format="DD/MM/YYYY").isoformat()
        hasta = d2.date_input("Coste hasta", date.fromisoformat(ph_), format="DD/MM/YYYY").isoformat()
    modo = "origen" if modo_lbl == "A origen" else "mes"
    df = oc.rentabilidad(con, obra_id, cid, modo, estados, desde, hasta)
    per = df.attrs.get("periodo", (None, None))

    venta, coste = int(df["certificado_m"].sum()), int(df["coste_m"].sum())
    contratado = int(df["contratado_m"].sum())
    k = st.columns(5)
    k[0].metric("Venta certificada", e(venta), help="Importe certificado al cliente en el modo elegido.")
    k[1].metric("Coste imputado", e(coste), help="Bases imponibles de facturas + costes manuales/importados.")
    k[2].metric("Margen", e(venta - coste), f"{(venta - coste) / venta * 100:.1f} %".replace(".", ",") if venta else None)
    k[3].metric("Contratado (adjudicado)", e(contratado))
    k[4].metric("Pendiente de facturar s/ contratos", e(int(df["pendiente_contrato_m"].fillna(0).clip(lower=0).sum())))
    st.caption(f"Periodo de coste: {per[0] or 'inicio'} → {per[1]} · Venta: {'a origen' if modo == 'origen' else 'del mes'} "
               f"de la certificación nº {cert['numero']}")

    cob = oc.cobertura(con, obra_id, cert)
    alertas = oc.alertas_rentabilidad(df, cob if modo == "origen" else 1.0)
    if alertas:
        with st.expander(f"Alertas ({len(alertas)})", expanded=True):
            for sev, msg in alertas:
                st.markdown(f"{icono_sev(sev)} {msg}")

    tabs = st.tabs(["Por capítulo", "Detalle de un capítulo", "Evolución mensual", "Relacionar facturas",
                    "Precio de venta vs coste", "Costes manuales / SIS"])

    # ---------------------------------------------------------------- por capítulo
    with tabs[0]:
        vis = df[(df["certificado_m"] != 0) | (df["coste_m"] != 0) | (df["contratado_m"] != 0)].copy()
        solo_coste = st.toggle("Ver solo capítulos con coste", value=bool(coste) and cob < 0.5)
        if solo_coste:
            vis = vis[vis["coste_m"] != 0]
        vis["lbl"] = vis["codigo"] + " · " + vis["capitulo"].str.title().str[:38]
        fig = go.Figure()
        fig.add_bar(y=vis["lbl"], x=vis["certificado_m"] / 1000, name="Venta certificada", orientation="h", marker_color=GRIS)
        fig.add_bar(y=vis["lbl"], x=vis["coste_m"] / 1000, name="Coste", orientation="h", marker_color=ROJO)
        if vis["contratado_m"].sum():
            fig.add_scatter(y=vis["lbl"], x=vis["contratado_m"] / 1000, name="Contratado", mode="markers",
                            marker=dict(symbol="line-ns", size=18, line=dict(width=3, color=OSCURO)))
        fig.update_layout(barmode="group", height=max(320, 34 * len(vis)), margin=dict(l=10, r=10, t=10, b=10),
                          yaxis=dict(autorange="reversed"), xaxis_title="€", legend=dict(orientation="h"), separators=",.")
        st.plotly_chart(fig, width="stretch")
        t = vis[["codigo", "capitulo", "responsable", "venta_prevista_m", "avance_pct", "certificado_m", "coste_facturas_m",
                 "coste_manual_m", "coste_m", "venta_interna_m", "margen_m", "margen_pct", "ppto_estudio_m", "coste_esperado_m", "desv_estudio_m",
                 "contratado_m", "margen_previsto_m", "pendiente_contrato_m"]].copy()
        for col in [c for c in t.columns if c.endswith("_m")]:
            t[col] = t[col].astype(float) / 1000
        st.dataframe(t, hide_index=True, width="stretch", column_config={
            "codigo": "Cap.", "capitulo": "Capítulo", "responsable": "Responsable",
            "venta_prevista_m": eur_col("Venta prevista (€)", "Deducida del % a origen de cada partida"),
            "avance_pct": st.column_config.ProgressColumn("Avance", format="%.1f %%", min_value=0, max_value=100),
            "certificado_m": eur_col("Certificado (€)"), "coste_facturas_m": eur_col("Facturas (€)"),
            "coste_manual_m": eur_col("Manual/SIS (€)"), "coste_m": eur_col("Coste total (€)"),
            "margen_m": eur_col("Margen (€)"), "margen_pct": st.column_config.NumberColumn("Margen %", format="%.1f %%"),
            "contratado_m": eur_col("Contratado (€)"),
            "venta_interna_m": eur_col("Venta interna (€)", "Obra ejecutada sin certificar, revisión de precios, extras aprobados… (solo uso interno)"),
            "ppto_estudio_m": eur_col("Ppto. coste Estudios (€)", "Presupuesto de coste importado en Obras y partidas"),
            "coste_esperado_m": eur_col("Coste esperado (€)", "Presupuesto de Estudios × % de avance"),
            "desv_estudio_m": eur_col("Desviación s/ Estudios (€)", "Coste real − coste esperado. Positivo = gastando más de lo previsto"),
            "margen_previsto_m": eur_col("Margen previsto (€)", "Venta prevista − contratado"),
            "pendiente_contrato_m": eur_col("Pendiente contrato (€)", "Contratado − ya facturado")})

    # ---------------------------------------------------------------- detalle
    with tabs[1]:
        opciones = df["codigo"].tolist()
        cap = st.selectbox("Capítulo", opciones, format_func=lambda x: f"{x} · {df.set_index('codigo').loc[x, 'capitulo']}")
        r = df.set_index("codigo").loc[cap]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        k = st.columns(4)
        k[0].metric("Certificado", e(r["certificado_m"]))
        k[1].metric("Coste", e(r["coste_m"]))
        k[2].metric("Margen", e(r["margen_m"]), f"{r['margen_pct']:.1f} %".replace(".", ",") if pd.notna(r["margen_pct"]) else None)
        k[3].metric("Contratado", e(r["contratado_m"]))
        a, b = st.columns(2)
        with a:
            st.markdown("**Facturas imputadas**")
            from core.analytics import lineas_coste_df
            lin = lineas_coste_df(con, obra_id=obra_id, desde=per[0], hasta=per[1], estados=estados)
            lin = lin[lin["partida_id"] == r["partida_id"]] if pd.notna(r["partida_id"]) else lin[lin["partida_id"].isna()]
            lin = lin.assign(importe=lin["importe_c"] / 100)
            st.dataframe(lin[["fecha", "proveedor", "numero", "descripcion", "importe"]], hide_index=True, width="stretch",
                         column_config={"importe": eur_col("Importe (€)")})
            if not lin.empty:
                g = lin.groupby("proveedor")["importe"].sum().sort_values(ascending=False)
                st.caption("Quién ejecuta (por coste): " + " · ".join(f"{p} {fmt_eur(v)}" for p, v in g.items()))
        with b:
            st.markdown("**Ofertas y contratos**")
            of = oc.ofertas_df(con, obra_id)
            of = of[of["partida_id"] == r["partida_id"]] if not of.empty and pd.notna(r["partida_id"]) else of.iloc[0:0]
            if of.empty:
                st.caption("Sin ofertas registradas. Añádelas en  Contratación.")
            else:
                of = of.assign(importe=of["importe_cents"] / 100)
                st.dataframe(of[["proveedor", "estado", "importe", "valorado_por", "puntuacion", "opinion"]], hide_index=True,
                             width="stretch", column_config={"importe": eur_col("Importe (€)"),
                                                             "puntuacion": st.column_config.NumberColumn("Valoración", format="%d / 5")})
            st.markdown("**Partidas de la certificación con movimiento**")
            cl = oc.cert_lineas_df(con, cid)
            cl = cl[(cl["capitulo"] == cap) & ((cl["actual_m"] != 0) if modo == "mes" else (cl["origen_m"] != 0))]
            cl = cl.assign(importe=(cl["actual_m"] if modo == "mes" else cl["origen_m"]) / 1000)
            st.dataframe(cl[["codigo", "titulo", "pct_origen", "precio", "importe"]].sort_values("importe", ascending=False),
                         hide_index=True, width="stretch", height=260,
                         column_config={"importe": eur_col("Certificado (€)"), "pct_origen": "% origen", "precio": "Precio"})
        with st.form(f"resp_{cap}"):
            p = db.one(con, "SELECT * FROM partidas WHERE id=?", (int(r["partida_id"]),)) if pd.notna(r["partida_id"]) else None
            if p:
                c1, c2 = st.columns([1, 2])
                resp = c1.text_input("Responsable del capítulo", p.get("responsable") or "")
                notas = c2.text_input("Notas / opinión", p.get("notas") or "")
                if st.form_submit_button("Guardar"):
                    with db.tx(con):
                        con.execute("UPDATE partidas SET responsable=?, notas=? WHERE id=?", (resp, notas, p["id"]))
                        db.audit(con, usuario(), "editar_partida", "partida", p["id"], {"responsable": resp, "notas": notas})
                    st.rerun()
            else:
                st.caption("Este capítulo no tiene partida de coste asociada. Usa «Estructura de coste» en Certificaciones.")
                st.form_submit_button("Guardar", disabled=True)

    # ---------------------------------------------------------------- mensual
    with tabs[2]:
        s = oc.serie_mensual(con, obra_id)
        fig = go.Figure()
        fig.add_bar(x=s["cert"].astype(str), y=s["venta_mes_m"] / 1000, name="Venta del mes", marker_color=GRIS)
        fig.add_bar(x=s["cert"].astype(str), y=s["coste_mes_m"] / 1000, name="Coste facturado del periodo", marker_color=ROJO)
        fig.add_scatter(x=s["cert"].astype(str), y=s["margen_mes_m"] / 1000, name="Margen del mes", mode="lines+markers",
                        line=dict(color=VERDE))
        fig.update_layout(barmode="group", height=360, margin=dict(l=10, r=10, t=10, b=10), xaxis_title="Nº certificación",
                          yaxis_title="€", legend=dict(orientation="h"), separators=",.")
        st.plotly_chart(fig, width="stretch")
        s2 = s.copy()
        for col in [c for c in s2.columns if c.endswith("_m")]:
            s2[col] = s2[col] / 1000
        st.dataframe(s2, hide_index=True, width="stretch", column_config={
            "venta_mes_m": eur_col("Venta mes (€)"), "venta_origen_m": eur_col("Venta origen (€)"),
            "coste_mes_m": eur_col("Coste periodo (€)"), "margen_mes_m": eur_col("Margen mes (€)")})
        st.caption("El coste del periodo es la suma de bases de las facturas con fecha entre la certificación anterior y esta.")

    # ---------------------------------------------------------------- relacionar
    with tabs[3]:
        _relacionar(con, obra_id)

    # ---------------------------------------------------------------- precios
    with tabs[4]:
        st.caption("Líneas de factura relacionadas con una partida de la certificación: cuánto se paga por unidad frente a "
                   "cuánto se cobra. Útil para detectar partidas vendidas por debajo del coste y para alimentar a Estudios.")
        pv = oc.precios_venta_vs_coste(con, obra_id)
        if pv.empty:
            st.info("Aún no hay líneas relacionadas con partidas de la certificación (pestaña «Relacionar facturas»).")
        else:
            pv["precio_coste_n"] = pd.to_numeric(pv["precio_coste"], errors="coerce")
            pv["precio_venta_n"] = pd.to_numeric(pv["precio_venta"], errors="coerce")
            pv["margen_unit_pct"] = ((pv["precio_venta_n"] - pv["precio_coste_n"]) / pv["precio_venta_n"] * 100).round(1)
            st.dataframe(pv.drop(columns=["precio_coste", "precio_venta", "importe_cents"]), hide_index=True, width="stretch",
                         column_config={"precio_coste_n": st.column_config.NumberColumn("Precio coste", format="localized"),
                                        "precio_venta_n": st.column_config.NumberColumn("Precio venta", format="localized"),
                                        "margen_unit_pct": st.column_config.NumberColumn("Margen unitario %", format="%.1f %%"),
                                        "cert_confianza": st.column_config.NumberColumn("Similitud", format="%.2f")})
            st.caption(" Compara solo si las unidades coinciden (m², ud…): una factura por horas o a tanto alzado no es comparable.")

    # ---------------------------------------------------------------- manuales
    with tabs[5]:
        _costes_manuales(con, obra_id)


def _relacionar(con, obra_id):
    st.caption("Cada línea de factura se imputa a un **capítulo** (para el margen) y, opcionalmente, a una **partida concreta de la "
               "certificación** (para comparar precios unitarios). Corrige aquí de forma masiva; los cambios quedan auditados.")
    partidas = maestros.partidas_de_obra(con, obra_id)
    lab = {p["id"]: f"{p['codigo']} · {p['descripcion'][:40]}" for p in partidas}
    inv = {v: k for k, v in lab.items()}
    lin = pd.DataFrame(db.rows(con, """
        SELECT l.id, d.id AS doc, d.fecha, COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, d.numero, l.descripcion,
               l.importe_cents, l.partida_id, l.partida_origen, l.partida_confianza, l.cert_partida, l.cert_confianza
        FROM lineas l JOIN documentos d ON d.id=l.documento_id LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
        WHERE d.obra_id=? AND d.estado<>'rechazada' AND d.tipo_documento IN ('factura','abono','anticipo')
        ORDER BY l.partida_confianza IS NOT NULL, l.partida_confianza, d.fecha""", (obra_id,)))
    if lin.empty:
        st.info("No hay líneas de factura en esta obra.")
        return
    c1, c2, c3 = st.columns([2, 2, 2])
    ver = c1.selectbox("Ver", ["Todas", "Sin capítulo o en «Sin asignar»", "Confianza baja (< 60 %)", "Sin partida de certificación"])
    umbral = c2.slider("Umbral de similitud para relacionar automáticamente", 0.4, 0.95, 0.6, 0.05)
    if c3.button("Relacionar automáticamente", icon=":material/bolt:", help="Solo líneas sin partida de certificación; nunca pisa lo manual."):
        n = oc.relacionar_automatico(con, obra_id, umbral, usuario())
        st.toast(f"{n} líneas relacionadas"); st.rerun()
    p99 = next((p["id"] for p in partidas if p["codigo"] == "99"), None)
    if ver.startswith("Sin capítulo"):
        lin = lin[lin["partida_id"].isna() | (lin["partida_id"] == p99)]
    elif ver.startswith("Confianza"):
        lin = lin[lin["partida_confianza"].fillna(0) < 0.6]
    elif ver.startswith("Sin partida"):
        lin = lin[lin["cert_partida"].isna() | (lin["cert_partida"] == "")]
    lin = lin.head(300).copy()
    lin["capitulo"] = lin["partida_id"].map(lab)
    lin["importe"] = lin["importe_cents"] / 100
    sug = {r.id: oc.sugerir_partida_cert(con, obra_id, r.descripcion or "", 1) for r in lin.itertuples()}
    lin["sugerencia"] = lin["id"].map(lambda i: f"{sug[i][0]['codigo']} · {sug[i][0]['titulo'][:45]} ({sug[i][0]['score']:.0%})"
                                      if sug.get(i) else "")
    ed = st.data_editor(lin[["id", "fecha", "proveedor", "numero", "descripcion", "importe", "capitulo", "partida_origen",
                             "cert_partida", "sugerencia"]], hide_index=True, width="stretch", key=f"rel_{obra_id}_{ver}",
                        disabled=["id", "fecha", "proveedor", "numero", "descripcion", "importe", "partida_origen", "sugerencia"],
                        column_config={"importe": eur_col("Importe (€)"),
                                       "capitulo": st.column_config.SelectboxColumn("Capítulo de coste", options=list(lab.values()),
                                                                                    width="medium"),
                                       "partida_origen": "Origen", "cert_partida": "Partida cert.",
                                       "descripcion": st.column_config.TextColumn("Concepto", width="large")})
    cambios = []
    orig = lin.set_index("id")
    for r in ed.itertuples():
        o = orig.loc[r.id]
        if r.capitulo != o["capitulo"] or (r.cert_partida or "") != (o["cert_partida"] or ""):
            cambios.append((int(r.id), inv.get(r.capitulo), r.cert_partida or None, r.capitulo != o["capitulo"]))
    b1, b2 = st.columns([1, 3])
    if b1.button(f"Guardar {len(cambios)} cambio(s)", type="primary", disabled=not cambios):
        with db.tx(con):
            for lid, pid, cp, cambia_cap in cambios:
                if cambia_cap:
                    con.execute("UPDATE lineas SET partida_id=?, partida_origen='manual', partida_confianza=1 WHERE id=?", (pid, lid))
                con.execute("UPDATE lineas SET cert_partida=?, cert_confianza=CASE WHEN ? IS NULL THEN NULL ELSE 1 END WHERE id=?",
                            (cp, cp, lid))
            db.audit(con, usuario(), "imputacion_masiva", "obra", obra_id, {"cambios": cambios})
        st.toast("Imputación guardada"); st.rerun()
    if b2.button("Aceptar las sugerencias visibles como partida de certificación"):
        with db.tx(con):
            n = 0
            for r in lin.itertuples():
                if sug.get(r.id) and not r.cert_partida:
                    con.execute("UPDATE lineas SET cert_partida=?, cert_confianza=? WHERE id=?",
                                (sug[r.id][0]["codigo"], sug[r.id][0]["score"], r.id))
                    n += 1
            db.audit(con, usuario(), "aceptar_sugerencias_cert", "obra", obra_id, {"lineas": n})
        st.rerun()


def _costes_manuales(con, obra_id):
    st.caption("Para costes que no llegan por factura de proveedor (mano de obra propia, maquinaria propia, costes indirectos) "
               "o para cargar el **coste a origen desde SIS** y así poder medir el margen a origen de toda la obra.")
    partidas = maestros.partidas_de_obra(con, obra_id)
    lab = {p["id"]: f"{p['codigo']} · {p['descripcion'][:40]}" for p in partidas}
    with st.form("cm_nuevo", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns([2, 3, 1.3, 1.3])
        pid = c1.selectbox("Capítulo", list(lab), format_func=lambda i: lab[i])
        concepto = c2.text_input("Concepto", placeholder="Mano de obra propia agosto")
        imp = c3.text_input("Importe (€)", placeholder="12500,00")
        fecha = c4.date_input("Fecha", date.today(), format="DD/MM/YYYY")
        if st.form_submit_button("Añadir coste"):
            if not imp.strip():
                st.error("Indica el importe.")
            else:
                with db.tx(con):
                    cur = con.execute("INSERT INTO costes_manuales (obra_id, partida_id, concepto, importe_cents, fecha, fuente, creado_por, creado_en) "
                                      "VALUES (?,?,?,?,?,?,?,?)", (obra_id, pid, concepto, to_cents(parse_amount(imp)), fecha.isoformat(),
                                                                   "manual", usuario(), db.now_iso()))
                    db.audit(con, usuario(), "coste_manual", "coste", cur.lastrowid, {"importe": imp, "concepto": concepto})
                st.rerun()
    st.markdown("**Importar desde Excel/CSV** (columnas: `capitulo`, `importe`, `fecha`, `concepto` opcional)")
    f = st.file_uploader("Costes", type=["xlsx", "csv"], key="cm_up", label_visibility="collapsed")
    if f and st.button("Importar costes"):
        dfx = pd.read_csv(f, sep=None, engine="python", dtype=str) if f.name.endswith(".csv") else pd.read_excel(f, dtype=str)
        dfx.columns = [c.strip().lower() for c in dfx.columns]
        por_cod = {p["codigo"]: p["id"] for p in partidas}
        n, err = 0, []
        with db.tx(con):
            for r in dfx.fillna("").to_dict("records"):
                pid = por_cod.get(str(r.get("capitulo", "")).strip())
                if not pid:
                    err.append(str(r.get("capitulo")))
                    continue
                con.execute("INSERT INTO costes_manuales (obra_id, partida_id, concepto, importe_cents, fecha, fuente, creado_por, creado_en) "
                            "VALUES (?,?,?,?,?,?,?,?)", (obra_id, pid, r.get("concepto") or "Importado", to_cents(parse_amount(r["importe"])),
                                                         pd.to_datetime(r["fecha"], dayfirst=True).date().isoformat() if r.get("fecha") else None,
                                                         f"import:{f.name}", usuario(), db.now_iso()))
                n += 1
            db.audit(con, usuario(), "importar_costes", "obra", obra_id, {"filas": n, "archivo": f.name})
        st.success(f"{n} costes importados." + (f" Capítulos no encontrados: {', '.join(sorted(set(err)))}" if err else ""))
    cm = pd.DataFrame(db.rows(con, "SELECT c.id, p.codigo AS capitulo, c.concepto, c.importe_cents, c.fecha, c.fuente, c.creado_por "
                                   "FROM costes_manuales c LEFT JOIN partidas p ON p.id=c.partida_id WHERE c.obra_id=? ORDER BY c.fecha DESC",
                                   (obra_id,)))
    if not cm.empty:
        cm["importe"] = cm["importe_cents"] / 100
        st.dataframe(cm.drop(columns=["importe_cents"]), hide_index=True, width="stretch",
                     column_config={"importe": eur_col("Importe (€)")})
        borrar = st.multiselect("Eliminar", cm["id"].tolist(), format_func=lambda i: f"#{i}")
        if borrar and st.button("Eliminar seleccionados"):
            with db.tx(con):
                con.execute(f"DELETE FROM costes_manuales WHERE id IN ({','.join('?' * len(borrar))})", borrar)
                db.audit(con, usuario(), "borrar_costes_manuales", "obra", obra_id, {"ids": borrar})
            st.rerun()
