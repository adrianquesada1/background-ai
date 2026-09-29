"""Rentabilidad por oficio: venta certificada (sin pase) frente a coste por industrial, con criterio prudente y proyección."""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import db, auditoria as A, obra_control as oc
from core.money import fmt_eur
from vistas.comun import get_con, usuario, eur_col, selector_obra

ROJO, GRAFITO, GRIS, VERDE = "#E1251B", "#2E2E2E", "#B5B3AA", "#2E7D4F"


def e(v):
    return fmt_eur(A.r2(v))


def p(v):
    return f"{A.r2(v):.2f} %".replace(".", ",")


def f(v):
    return float(A.r2(v))


def render():
    con = get_con()
    A.init(con)
    st.title("Rentabilidad por oficio")
    st.caption("La venta se certifica por capítulos y el coste llega por industrial: el oficio une ambos. Por oficio se compara "
               "lo certificado sin pase con lo que cuesta; en los oficios en curso, lo cobrado por delante de lo pagado se trata "
               "como coste pendiente (criterio prudente).")
    obra_id = selector_obra("aud_obra", permitir_todas=False)
    if not obra_id:
        return
    obra = db.one(con, "SELECT * FROM obras WHERE id=?", (obra_id,))
    certs = oc.certificaciones(con, obra_id)
    A.asegurar_oficios(con, obra_id)
    if certs:
        # propuestas automáticas: partidas y proveedores sin oficio (no pisa lo confirmado)
        if A.criterios_cert(con, obra_id, certs[-1]["id"])["nueva"].any():
            A.clasificar_partidas_auto(con, obra_id, certs[-1]["id"])
        A.clasificar_proveedores_auto(con, obra_id)
    tiene_datos = True
    listo = bool(certs) and tiene_datos
    if listo:
        lab = {c["id"]: f"nº {c['numero']} · {c['fecha']}" for c in certs}
        hay_sis = db.one(con, "SELECT COUNT(*) n FROM compras_sis WHERE obra_id=?", (obra_id,))["n"] > 0
        c1, c2, c3 = st.columns([2, 3, 3])
        cid = c1.selectbox("Certificación", list(lab), index=len(lab) - 1, format_func=lambda i: lab[i], key="aud_cert")
        modo_lbl = c2.radio("Periodo", ["Mes de la certificación", "A origen"], horizontal=True, index=0 if not hay_sis else 1,
                            help="Mes: certificado del mes frente al coste con fecha en su periodo. A origen: acumulado de la obra.")
        modo = "mes" if modo_lbl.startswith("Mes") else "origen"
        con_app = c3.toggle("Sumar facturas registradas aquí que aún no están en SIS", value=False, disabled=not hay_sis,
                            help="Anticipa costes ya recibidos pero no contabilizados en SIS (se detectan por número de factura).")
        cert = next(c for c in certs if c["id"] == cid)
        con_dev = st.toggle("Incluir coste devengado sin factura (certificaciones y albaranes de proveedor)", value=False,
                            key="aud_dev", help="Obra ejecutada o material recibido cuya factura aún no ha llegado. Es la base de la "
                                                "provisión de cierre y evita que el margen parezca mejor de lo que es.")
        r = A.calcular(con, obra_id, cid, None, con_app, modo, con_dev)
        r_origen = r if modo == "origen" else A.calcular(con, obra_id, cid, None, con_app, "origen")
        pr = A.proyeccion(r_origen)
        cob, desc_cob = A.cobertura_coste(con, obra_id, r)
        per = r["periodo"]
        st.caption(f"Coste: {'exportación de compras de SIS' if r['fuente_coste'] == 'SIS' else 'facturas registradas en la aplicación'}"
                   + (f" · periodo {per[0]} a {per[1]}" if modo == "mes" else " · acumulado a origen"))
        if modo == "origen" and cob < 0.6:
            st.warning(f"{desc_cob}: cubren poco periodo frente a {cert['numero']} certificaciones, así que el margen a origen "
                       "saldría falsamente alto. Use «Mes de la certificación» o cargue la exportación de compras de SIS "
                       "en la pestaña «Datos de SIS».")
    tabs = st.tabs(["Resumen", "Por oficio", "Evolución", "Proyección", "Oficio de cada partida",
                    "Oficio de cada proveedor y personal", "Datos de SIS"])
    with tabs[6]:
        _importar(con, obra_id, certs)
    if not certs:
        with tabs[0]:
            st.info("Importe primero la certificación del cliente en Certificaciones.")
        return
    if not tiene_datos:
        with tabs[0]:
            st.info("Cargue datos en la pestaña «Datos de SIS».")
        return

    # ================================================================ resumen
    with tabs[0]:
        k = st.columns(4)
        k[0].metric("Venta certificada" + (" del mes" if modo == "mes" else " a origen"), e(r["ventas"]))
        k[1].metric("Coste de compras", e(r["compras"]))
        k[2].metric("Personal propio (RRHH)", e(r["rrhh"]))
        k[3].metric("Resultado bruto", e(r["resultado_sis"]), p(r["pct_sis"]), delta_color="off")
        k = st.columns(4)
        k[0].metric("Ajuste prudente (oficios en curso)", e(r["correccion"]), help="Lo cobrado sin pase por delante de lo pagado en oficios no terminados se considera coste pendiente.")
        k[1].metric("Resultado prudente", e(r["resultado"]), p(r["pct"]), delta_color="off")
        cob_o, _ = A.cobertura_coste(con, obra_id, r_origen)
        if cob_o >= 0.6:
            k[2].metric("Proyección a fin de obra", e(pr["proyeccion"]), p(pr["pct"]), delta_color="off")
        else:
            k[2].metric("Proyección a fin de obra", "—", help="Necesita el coste a origen completo (exportación de compras de SIS).")
        if r.get("internos"):
            ri_ = r["internos"]
            st.caption(f"Incluye costes internos: mano de obra propia {e(ri_['mano_obra'])}, otros costes {e(ri_['coste'])}, "
                       f"cargos a subcontratas −{e(ri_['cargos'])} y ventas internas {e(ri_['venta'])}"
                       + (f" ({ri_['borradores']} movimiento(s) aún sin validar)." if ri_["borradores"] else "."))
        k[3].metric("Devengado sin factura (provisión)", e(r["devengado"]),
                    help=f"{len(r['devengado_docs'])} certificación(es)/albarán(es) de proveedor sin factura asociada." +
                         (" Incluido en el coste." if r["devengado_incluido"] else " No incluido en el coste (actívelo arriba)."))
        k[3].caption(f"Facturación pendiente del cliente: {e(pr['facturacion_pendiente'])}")
        if False:
            k[3].metric("Facturación pendiente", e(pr["facturacion_pendiente"]),
                    help="Presupuesto de venta − certificado a origen." + (" Presupuesto estimado desde la certificación: "
                                                                           "introdúzcalo en Proyección." if r_origen["parametros"]["ppto_estimado"] else ""))

        controles = []
        controles.append((r["pct_ventas_clasif"] >= 99.99, f"Venta asignada a oficios: {p(r['pct_ventas_clasif'])}"))
        controles.append((r["pct_compras_clasif"] >= 99.99, f"Coste asignado a oficios: {p(r['pct_compras_clasif'])}"))
        controles.append((r["partidas_sin_criterio"] == 0, f"Partidas certificadas sin oficio: {r['partidas_sin_criterio']}"))
        controles.append((not r["proveedores_sin_categoria"], f"Proveedores sin oficio: {len(r['proveedores_sin_categoria'])}"))
        with st.container(border=True):
            st.markdown("**Controles de calidad del cálculo**")
            for ok, txt in controles:
                st.markdown((":green[:material/check_circle:] " if ok else ":orange[:material/warning:] ") + txt)
            if r["facturas_app_sin_sis"]:
                st.markdown(f":gray[:material/info:] Se han sumado {r['facturas_app_sin_sis']} factura(s) registradas aquí y aún no en SIS.")
        t = pd.DataFrame([
            ("Venta certificada", f(r["ventas"]), float("nan")),
            ("Coste de compras", -f(r["compras"]), float("nan")),
            ("Personal propio", -f(r["rrhh"]), float("nan")),
            ("Resultado bruto", f(r["resultado_sis"]), float(r["pct_sis"])),
            ("Ajuste prudente por oficios en curso", f(r["correccion"]), float("nan")),
            ("Resultado prudente", f(r["resultado"]), float(r["pct"])),
        ], columns=["Concepto", "Importe", "% s/ ventas"])
        st.dataframe(t, hide_index=True, width="stretch", column_config={
            "Importe": eur_col("Importe (€)"), "% s/ ventas": st.column_config.NumberColumn("% s/ ventas", format="%.2f %%")})
        b1, b2, _ = st.columns([2, 2, 4])
        if modo == "origen" and b1.button("Cerrar el mes en el histórico", help="Guarda las cifras a origen de este mes para la evolución."):
            A.guardar_mes(con, obra_id, r, usuario())
            st.success(f"Mes {r['corte'][:7]} guardado en el histórico.")
        xlsx = A.exportar_excel(r_origen, A.historico(con, obra_id, r_origen), pr, obra)
        b2.download_button("Descargar informe Excel", xlsx, file_name=f"auditoria_{obra['codigo']}_{cert['fecha'][:7]}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           help="Informe con fórmulas vivas: se puede revisar y modificar en Excel.")

    # ================================================================ categorías
    with tabs[1]:
        cats = pd.DataFrame(r["categorias"])
        dirs = cats[(cats["tipo"] == "DIRECTO") & (cats["cobrado"] != 0)].copy()
        dirs = dirs.sort_values("cobrado", key=lambda s: s.map(float), ascending=False)
        fig = go.Figure()
        fig.add_bar(y=dirs["categoria"], x=dirs["sin_pase"].map(float), name="Certificado sin pase", orientation="h", marker_color=GRIS)
        fig.add_bar(y=dirs["categoria"], x=dirs["pagado"].map(float), name="Coste", orientation="h", marker_color=ROJO)
        fig.update_layout(barmode="group", height=max(360, 30 * len(dirs)), margin=dict(l=10, r=10, t=10, b=10),
                          yaxis=dict(autorange="reversed"), xaxis_title="€", legend=dict(orientation="h"), separators=",.")
        st.plotly_chart(fig, width="stretch")
        st.caption(f"Pase aplicado: {p(r['parametros']['pase'] * 100)} (se edita en Proyección). Diferencial = certificado sin pase − coste. "
                   "En los oficios en curso el diferencial se descuenta del resultado (coste pendiente de llegar); "
                   "en los terminados se considera realizado. Marque «Terminado» cuando el oficio esté cerrado.")
        vis = cats.copy()
        for c in ("cobrado", "sin_pase", "pagado", "diferencial", "correccion"):
            vis[c] = vis[c].map(float)
        vis["margen_pct"] = vis["margen_pct"].map(lambda x: float(x) if x is not None else None)
        ed = st.data_editor(vis[["id", "categoria", "venta", "tipo", "terminada", "cobrado", "sin_pase", "pagado", "diferencial",
                                 "correccion", "margen_pct"]], hide_index=True, width="stretch", key=f"aud_cats_{cid}",
                            disabled=["id", "categoria", "venta", "cobrado", "sin_pase", "pagado", "diferencial", "correccion", "margen_pct"],
                            column_config={
                                "id": None, "categoria": "Oficio", "venta": None,
                                "tipo": st.column_config.SelectboxColumn("Tipo", options=["DIRECTO", "INDIRECTO"]),
                                "terminada": st.column_config.CheckboxColumn("Terminado", help="Oficio cerrado: su diferencial se da por realizado"),
                                "cobrado": eur_col("Certificado (€)"), "sin_pase": eur_col("Sin pase (€)"), "pagado": eur_col("Coste (€)"),
                                "diferencial": eur_col("Diferencial (€)"), "correccion": eur_col("Ajuste prudente (€)"),
                                "margen_pct": st.column_config.NumberColumn("Margen s/ cobrado", format="%.1f %%")})
        cambios = [(int(a.id), a.tipo, int(bool(a.terminada))) for a, b in zip(ed.itertuples(), vis.itertuples())
                   if a.tipo != b.tipo or bool(a.terminada) != bool(b.terminada)]
        if st.button(f"Guardar {len(cambios)} cambio(s)", disabled=not cambios, type="primary", key="aud_cat_save"):
            with db.tx(con):
                for i, t_, te in cambios:
                    con.execute("UPDATE aud_categorias SET tipo=?, terminada=? WHERE id=?", (t_, te, i))
                db.audit(con, usuario(), "categorias_auditoria", "obra", obra_id, {"cambios": cambios})
            st.rerun()

    # ================================================================ evolución
    with tabs[2]:
        h = A.historico(con, obra_id, r_origen)
        if h.empty:
            st.info("Sin histórico.")
        else:
            fig = go.Figure()
            for col, nom, color in (("ventas", "Ventas", GRAFITO), ("compras", "Compras", ROJO), ("rrhh", "Personal", GRIS)):
                fig.add_scatter(x=h["mes"], y=h[col].map(float), name=nom, mode="lines+markers", line=dict(color=color))
            fig.update_layout(height=340, margin=dict(l=10, r=10, t=30, b=10), title="A origen", yaxis_title="€",
                              legend=dict(orientation="h"), separators=",.")
            st.plotly_chart(fig, width="stretch")
            fig = go.Figure()
            fig.add_scatter(x=h["mes"], y=h["pct_sis"], name="% resultado SIS", mode="lines+markers", line=dict(color=GRIS))
            fig.add_scatter(x=h["mes"], y=h["pct"], name="% resultado corregido", mode="lines+markers", line=dict(color=ROJO, width=3))
            fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10), title="Margen sobre ventas", yaxis_title="%",
                              legend=dict(orientation="h"), separators=",.")
            st.plotly_chart(fig, width="stretch")
            hv = h.copy()
            for c in ("ventas", "compras", "rrhh", "resultado_sis", "correccion", "resultado"):
                hv[c] = hv[c].map(float)
            st.dataframe(hv, hide_index=True, width="stretch", column_config={
                **{c: eur_col(c.replace("_", " ").capitalize() + " (€)") for c in ("ventas", "compras", "rrhh", "resultado_sis",
                                                                                     "correccion", "resultado")},
                "pct_sis": st.column_config.NumberColumn("% SIS", format="%.2f %%"),
                "pct": st.column_config.NumberColumn("% corregido", format="%.2f %%"), "mes": "Mes", "fuente": "Origen"})

    # ================================================================ proyección
    with tabs[3]:
        pa = r_origen["parametros"]
        a, b = st.columns([1, 1], gap="large")
        with a:
            st.markdown("**Parámetros de la obra**")
            with st.form("aud_param"):
                vals = {}
                for campo, etiqueta, ayuda in (
                        ("pase", "Pase (GG + BI) en tanto por uno", "0,16 = 16 %"),
                        ("ppto_venta", "Presupuesto total de venta (€)", None),
                        ("cd", "Coste directo presupuestado (€)", None), ("ci", "Coste indirecto presupuestado (€)", None),
                        ("gg", "Gastos generales presupuestados (€)", None),
                        ("desv_pct", "Desviación de coste prevista (% s/ presupuesto)", "1 = 1 %"),
                        ("ind_pendiente", "Indirectos: presupuesto pendiente de consumir (€)", None),
                        ("ind_estimado", "Indirectos: coste estimado hasta fin (€)", None)):
                    vals[campo] = st.text_input(etiqueta, format(pa[campo], "f").replace(".", ","), help=ayuda)
                if st.form_submit_button("Guardar parámetros", type="primary"):
                    from core.money import parse_amount
                    with db.tx(con):
                        for k_, v_ in vals.items():
                            con.execute(f"UPDATE aud_parametros SET {k_}=?, actualizado_por=?, actualizado_en=? WHERE obra_id=?",
                                        (str(parse_amount(v_)), usuario(), db.now_iso(), obra_id))
                        db.audit(con, usuario(), "parametros_auditoria", "obra", obra_id, vals)
                    st.rerun()
        with b:
            st.markdown("**Proyección a fin de obra**")
            pasos = [("Resultado hoy", pr["resultado_hoy"]), ("Beneficio por indirectos", pr["bfo_indirectos"]),
                     ("Desviaciones de coste", pr["desviaciones"]), ("GG de la facturación pendiente", pr["gg_pendientes"])]
            fig = go.Figure(go.Waterfall(x=[x for x, _ in pasos] + ["Proyección"], y=[f(v) for _, v in pasos] + [0],
                                         measure=["absolute", "relative", "relative", "relative", "total"],
                                         connector=dict(line=dict(color=GRIS)), increasing=dict(marker_color=VERDE),
                                         decreasing=dict(marker_color=ROJO), totals=dict(marker_color=GRAFITO)))
            fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="€", separators=",.")
            st.plotly_chart(fig, width="stretch")
            st.dataframe(pd.DataFrame([
                ("Resultado hoy", f(pr["resultado_hoy"])), ("Beneficio por indirectos", f(pr["bfo_indirectos"])),
                ("Desviaciones de coste", f(pr["desviaciones"])), ("Facturación pendiente", f(pr["facturacion_pendiente"])),
                (f"GG pendientes ({p(pr['gg_pct'])})", f(pr["gg_pendientes"])), ("Proyección de resultado", f(pr["proyeccion"]))],
                columns=["Concepto", "Importe"]), hide_index=True, width="stretch", column_config={"Importe": eur_col("Importe (€)")})
            st.caption(f"Resultado de obra proyectado: {p(pr['pct'])} sobre el presupuesto de venta. "
                       "Parte del resultado prudente a origen del último mes.")
            if r_origen["parametros"]["ppto_estimado"]:
                st.info("El presupuesto de venta se ha estimado desde la certificación. Introduzca el real, los GG y los "
                        "indirectos para una proyección fiable.")
            if A.cobertura_coste(con, obra_id, r_origen)[0] < 0.6:
                st.warning("El coste a origen está incompleto (solo facturas recientes): esta proyección no es representativa.")

    # ================================================================ criterios
    with tabs[4]:
        _criterios(con, obra_id, cid)

    # ================================================================ proveedores y RRHH
    with tabs[5]:
        _proveedores_rrhh(con, obra_id, r)


def _importar(con, obra_id, certs):
    st.markdown("Opcional. Sin estos datos el coste sale de las facturas registradas en la aplicación. Con ellos, el análisis "
                "a origen es completo:\n\n- **Compras de SIS** (listado de facturas de compra de la obra).\n"
                "- **Partes de trabajo de SIS** (personal propio).\n\nSe reconocen solos por sus columnas. Son acumulados a origen: "
                "cada carga sustituye a la anterior. Un libro que además traiga hojas de oficios por proveedor, oficios por partida "
                "o un histórico mensual también se aprovecha.")
    fx = st.file_uploader("Libro Excel", type=["xlsx", "xlsm"], key="aud_up")
    if not certs:
        st.warning("Importe antes la certificación en PDF.")
    if fx and st.button("Importar", type="primary"):
        with st.spinner("Importando y enlazando criterios…"):
            res = A.importar_libro(con, obra_id, fx.getvalue(), fx.name, usuario(), certs[-1]["id"] if certs else None)
        filas = []
        for k_, et in (("compras", "Facturas de compra"), ("rrhh", "Partes de trabajo"), ("categorias", "Categorías de coste"),
                       ("proveedores_clasificados", "Proveedores clasificados"), ("categorias_analizadas", "Categorías analizadas"),
                       ("criterios_excel", "Criterios de partidas en el Excel"), ("criterios_enlazados", "Criterios enlazados"),
                       ("meses_historico", "Meses de histórico")):
            if k_ in res:
                filas.append((et, str(res[k_])))
        st.success("Importación completada.")
        st.dataframe(pd.DataFrame(filas, columns=["Dato", "Cantidad"]), hide_index=True)
        if res.get("categorias_terminadas"):
            st.caption("Categorías marcadas como terminadas en la hoja: " + ", ".join(res["categorias_terminadas"]))
        for a in res.get("avisos", []):
            st.warning(a)


def _criterios(con, obra_id, cid):
    cats = A.categorias(con, obra_id)
    venta = [c for c in cats if c["nombre_venta"]]
    lab = {c["id"]: c["nombre"] for c in venta}
    inv = {v: k for k, v in lab.items()}
    lin = A.criterios_cert(con, obra_id, cid)
    if lin.empty:
        st.info("Sin partidas.")
        return
    nuevas = lin[lin["nueva"]]
    if len(nuevas):
        st.warning(f"{len(nuevas)} partida(s) certificadas sin oficio. Se propone el oficio mayoritario de su subcapítulo.")
        if st.button("Aceptar todas las propuestas"):
            n = A.guardar_criterios(con, obra_id, [{"codigo": x.codigo, "titulo": x.titulo, "categoria_id": x.sugerida_id}
                                                   for x in nuevas.itertuples() if pd.notna(x.sugerida_id)], usuario(), "sugerido")
            st.toast(f"{n} criterios guardados")
            st.rerun()
    else:
        st.success("Todas las partidas con importe tienen oficio. Las marcadas como «Automático» conviene revisarlas una vez.")
    c1, c2 = st.columns(2)
    ver = c1.radio("Mostrar", ["Sin criterio", "Automáticas (por revisar)", "Todas"], horizontal=True, index=0 if len(nuevas) else 1)
    q = c2.text_input("Buscar", key="aud_crit_q")
    d = (nuevas if ver == "Sin criterio" else lin[(lin["origen_m"] != 0) & ((lin["criterio"] == "auto") if ver.startswith("Auto") else True)]).copy()
    if q:
        d = d[d.apply(lambda x: q.lower() in f"{x['codigo']} {x['titulo']}".lower(), axis=1)]
    d["categoria"] = d["categoria_id"].map(lambda i: lab.get(int(i)) if pd.notna(i) else None)
    d["propuesta"] = d["sugerida_id"].map(lambda i: lab.get(int(i)) if pd.notna(i) else None)
    d["importe"] = d["origen_m"] / 1000
    d["criterio"] = d["criterio"].map({"excel": "Importado", "auto": "Automático", "manual": "Manual", "sugerido": "Propuesto"}).fillna("")
    ed = st.data_editor(d[["capitulo", "subcapitulo", "codigo", "titulo", "importe", "categoria", "propuesta", "criterio"]].head(400),
                        hide_index=True, width="stretch", key=f"crit_{cid}_{ver}",
                        disabled=["capitulo", "subcapitulo", "codigo", "titulo", "importe", "propuesta", "criterio"],
                        column_config={"importe": eur_col("A origen (€)"), "criterio": "Origen",
                                       "categoria": st.column_config.SelectboxColumn("Oficio", options=list(lab.values()), width="medium")})
    cambios = [{"codigo": a.codigo, "titulo": a.titulo, "categoria_id": inv.get(a.categoria)}
               for a, b in zip(ed.itertuples(), d.head(400).itertuples()) if a.categoria != b.categoria and a.categoria]
    if st.button(f"Guardar {len(cambios)} criterio(s)", disabled=not cambios, type="primary", key="crit_save"):
        A.guardar_criterios(con, obra_id, cambios, usuario(), "manual")
        st.rerun()


def _proveedores_rrhh(con, obra_id, r):
    cats = A.categorias(con, obra_id)
    lab = {c["id"]: f"{c['nombre']} ({c['tipo'].lower()})" for c in cats}
    inv = {v: k for k, v in lab.items()}
    sin = r["proveedores_sin_categoria"]
    if sin:
        st.warning(f"{len(sin)} proveedor(es) sin oficio: su coste no entra en ningún oficio.")
    if r["fuente_coste"] == "SIS":
        pv = pd.DataFrame(db.rows(con, """SELECT c.proveedor, c.proveedor_norm, COUNT(*) AS facturas,
                                          SUM(CAST(c.base AS REAL)) AS base, pc.categoria_id, pc.origen
                                          FROM compras_sis c LEFT JOIN aud_proveedor_cat pc
                                          ON pc.obra_id=c.obra_id AND pc.proveedor_norm=c.proveedor_norm
                                          WHERE c.obra_id=? GROUP BY c.proveedor_norm ORDER BY base DESC""", (obra_id,)))
    else:
        pv = pd.DataFrame(db.rows(con, """SELECT COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, COUNT(*) AS facturas,
                                          SUM(d.base_imponible_cents)/100.0 AS base FROM documentos d
                                          LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
                                          WHERE d.obra_id=? AND d.estado<>'rechazada' AND d.tipo_documento IN ('factura','abono','anticipo')
                                          GROUP BY 1 ORDER BY base DESC""", (obra_id,)))
        if not pv.empty:
            pv["proveedor_norm"] = pv["proveedor"].map(A.norm)
            m = {x["proveedor_norm"]: x for x in db.rows(con, "SELECT proveedor_norm, categoria_id, origen FROM aud_proveedor_cat WHERE obra_id=?",
                                                         (obra_id,))}
            pv["categoria_id"] = pv["proveedor_norm"].map(lambda k: m[k]["categoria_id"] if k in m else None)
            pv["origen"] = pv["proveedor_norm"].map(lambda k: m[k]["origen"] if k in m else None)
    if not pv.empty:
        pv["categoria"] = pv["categoria_id"].map(lambda i: lab.get(int(i)) if pd.notna(i) else None)
        solo = st.toggle("Ver solo proveedores sin oficio", value=bool(sin))
        vis = pv[pv["categoria"].isna()] if solo else pv
        vis = vis.assign(origen=vis["origen"].map({"excel": "Importado", "auto": "Automático", "manual": "Manual"}).fillna(""))
        ed = st.data_editor(vis[["proveedor", "facturas", "base", "categoria", "origen"]], hide_index=True, width="stretch", key=f"pv_{solo}",
                            disabled=["proveedor", "facturas", "base", "origen"],
                            column_config={"base": eur_col("Base (€)"), "origen": "Origen",
                                           "categoria": st.column_config.SelectboxColumn("Oficio", options=list(lab.values()))})
        cambios = [(b.proveedor_norm, b.proveedor, inv.get(a.categoria)) for a, b in zip(ed.itertuples(), vis.itertuples())
                   if a.categoria != b.categoria and a.categoria]
        if st.button(f"Guardar {len(cambios)} clasificación(es)", disabled=not cambios, type="primary", key="pv_save"):
            with db.tx(con):
                for pn, pnom, cid_ in cambios:
                    con.execute("INSERT INTO aud_proveedor_cat (obra_id, proveedor_norm, proveedor, categoria_id, origen) VALUES (?,?,?,?,?) "
                                "ON CONFLICT(obra_id, proveedor_norm) DO UPDATE SET categoria_id=excluded.categoria_id, origen='manual'",
                                (obra_id, pn, pnom, cid_, "manual"))
                db.audit(con, usuario(), "clasificar_proveedores", "obra", obra_id, {"n": len(cambios)})
            st.rerun()
    st.markdown("**Personal propio (partes de trabajo de SIS)**")
    rr = pd.DataFrame(db.rows(con, "SELECT categoria, nombre, CAST(horas AS REAL) horas, CAST(coste AS REAL) coste, fecha "
                                   "FROM rrhh_sis WHERE obra_id=?", (obra_id,)))
    if rr.empty:
        st.caption("Sin partes de trabajo importados.")
        return
    g = rr.groupby("categoria").agg(personas=("nombre", "nunique"), horas=("horas", "sum"), coste=("coste", "sum")).reset_index()
    g = g.sort_values("coste", ascending=False)
    a, b = st.columns([1, 1])
    a.dataframe(g, hide_index=True, width="stretch", column_config={"coste": eur_col("Coste (€)"), "categoria": "Categoría",
                                                                    "horas": st.column_config.NumberColumn("Horas", format="%.1f")})
    rr["mes"] = rr["fecha"].str[:7]
    m = rr.groupby("mes")["coste"].sum().reset_index()
    fig = go.Figure(go.Bar(x=m["mes"], y=m["coste"], marker_color=GRAFITO))
    fig.update_layout(height=260, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="€ / mes", separators=",.")
    b.plotly_chart(fig, width="stretch")
    per = rr.groupby(["categoria", "nombre"]).agg(horas=("horas", "sum"), coste=("coste", "sum")).reset_index().sort_values("coste", ascending=False)
    st.dataframe(per, hide_index=True, width="stretch", column_config={"coste": eur_col("Coste (€)"), "nombre": "Persona",
                                                                       "categoria": "Categoría"})
