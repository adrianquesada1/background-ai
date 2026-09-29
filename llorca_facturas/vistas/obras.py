"""Maestro de obras y partidas (capítulos de coste) con presupuesto importable."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import db, maestros
from core.money import parse_amount, to_cents, from_cents, fmt_eur, D
from core.validation import validar_y_guardar
from vistas.comun import get_con, usuario, eur_col


def _importar_presupuesto(con, obra_id: int, archivo) -> tuple[int, int, list[str]]:
    """
    Importa un presupuesto desde Excel/CSV con columnas: codigo, descripcion, presupuesto_coste
    [, presupuesto_venta, palabras_clave]. Los importes se leen como TEXTO para no pasar por float.
    """
    if archivo.name.lower().endswith(".csv"):
        df = pd.read_csv(archivo, sep=None, engine="python", dtype=str)
    else:
        df = pd.read_excel(archivo, dtype=str)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    faltan = [c for c in ("codigo", "descripcion") if c not in df.columns]
    if faltan:
        return 0, 0, [f"Faltan columnas obligatorias: {', '.join(faltan)}"]
    nuevas = actualizadas = 0
    avisos = []
    with db.tx(con):
        for r in df.fillna("").to_dict("records"):
            cod = str(r["codigo"]).strip()
            if not cod:
                continue
            pc = to_cents(parse_amount(r.get("presupuesto_coste", ""))) if str(r.get("presupuesto_coste", "")).strip() else 0
            pv = to_cents(parse_amount(r.get("presupuesto_venta", ""))) if str(r.get("presupuesto_venta", "")).strip() else 0
            ex = db.one(con, "SELECT id FROM partidas WHERE obra_id=? AND codigo=?", (obra_id, cod))
            if ex:
                con.execute("UPDATE partidas SET descripcion=?, presupuesto_coste_cents=?, presupuesto_venta_cents=?, "
                            "palabras_clave=CASE WHEN ?<>'' THEN ? ELSE palabras_clave END WHERE id=?",
                            (r["descripcion"], pc, pv, r.get("palabras_clave", ""), r.get("palabras_clave", ""), ex["id"]))
                actualizadas += 1
            else:
                con.execute("INSERT INTO partidas (obra_id, codigo, descripcion, palabras_clave, presupuesto_coste_cents, "
                            "presupuesto_venta_cents) VALUES (?,?,?,?,?,?)",
                            (obra_id, cod, r["descripcion"], r.get("palabras_clave", ""), pc, pv))
                nuevas += 1
        db.audit(con, usuario(), "importar_presupuesto", "obra", obra_id,
                 {"archivo": archivo.name, "nuevas": nuevas, "actualizadas": actualizadas})
    return nuevas, actualizadas, avisos


def render():
    con = get_con()
    st.title("Obras y partidas")

    with st.expander(" Nueva obra"):
        with st.form("nueva_obra"):
            c1, c2, c3 = st.columns([1, 2, 2])
            cod = c1.text_input("Código *", placeholder="664")
            nom = c2.text_input("Nombre *", placeholder="Alibuilding Benidorm")
            cli = c3.text_input("Cliente / propiedad")
            alias = st.text_input("Alias (separados por comas)",
                                  help="Cómo aparece la obra en las facturas: abreviaturas, erratas habituales, nombre comercial…")
            c4, c5 = st.columns(2)
            pv = c4.text_input("Presupuesto de venta (€)", placeholder="1250000,00")
            pc = c5.text_input("Presupuesto de coste (€)", placeholder="1030000,00")
            confirmar = st.checkbox("Confirmo que es una obra distinta aunque se parezca a otra existente")
            if st.form_submit_button("Crear obra", type="primary"):
                from rapidfuzz import fuzz
                parecidas = [o for o in maestros.listar_obras(con, solo_activas=False)
                             if fuzz.token_set_ratio(maestros.strip_accents(o["nombre"]), maestros.strip_accents(nom)) >= 85
                             or (alias and any(a.strip() and a.strip().lower() in (o["alias"] or "").lower() for a in alias.split(",")))]
                if not cod.strip() or not nom.strip():
                    st.error("Código y nombre son obligatorios.")
                elif db.one(con, "SELECT id FROM obras WHERE codigo=?", (cod.strip(),)):
                    st.error("Ya existe una obra con ese código.")
                elif parecidas and not confirmar:
                    st.warning("Posible obra duplicada: " + ", ".join(f"{o['codigo']} · {o['nombre']}" for o in parecidas) +
                               ". Si de verdad es otra obra, marque la casilla de confirmación.")
                else:
                    maestros.crear_obra(con, cod, nom, cli, "", alias, parse_amount(pv or "0"), parse_amount(pc or "0"),
                                        usuario())
                    st.success("Obra creada con los 17 capítulos estándar."); st.rerun()

    obras = maestros.listar_obras(con, solo_activas=False)
    if not obras:
        st.info("No hay obras.")
        return
    obra_id = st.selectbox("Obra", [o["id"] for o in obras], key="obras_sel",
                           format_func=lambda i: next(f"{o['codigo']} · {o['nombre']}" for o in obras if o["id"] == i))
    o = db.one(con, "SELECT * FROM obras WHERE id=?", (obra_id,))
    vista = st.segmented_control("Vista", ["Ficha, responsables y partidas", "Facturas de la obra"], key="obras_vista",
                                 default="Ficha, responsables y partidas", label_visibility="collapsed")
    if vista == "Facturas de la obra":
        _facturas_obra(con, obra_id, o)
        return

    with st.form(f"edit_obra_{obra_id}"):
        c1, c2, c3 = st.columns([2, 2, 1])
        nom = c1.text_input("Nombre", o["nombre"])
        cli = c2.text_input("Cliente", o["cliente"] or "")
        activa = c3.checkbox("Activa", bool(o["activa"]))
        alias = st.text_area("Alias", o["alias"] or "", height=68)
        c4, c5 = st.columns(2)
        pv = c4.text_input("Presupuesto de venta (€)", format(from_cents(o["presupuesto_venta_cents"]), "f").replace(".", ","))
        pc = c5.text_input("Presupuesto de coste (€)", format(from_cents(o["presupuesto_coste_cents"]), "f").replace(".", ","),
                           help="Si lo dejas a 0 se usa la suma del presupuesto de las partidas.")
        if st.form_submit_button("Guardar obra"):
            with db.tx(con):
                con.execute("UPDATE obras SET nombre=?, cliente=?, activa=?, alias=?, presupuesto_venta_cents=?, "
                            "presupuesto_coste_cents=? WHERE id=?",
                            (nom, cli, int(activa), alias, to_cents(parse_amount(pv or "0")), to_cents(parse_amount(pc or "0")),
                             obra_id))
                db.audit(con, usuario(), "editar_obra", "obra", obra_id, {"nombre": nom, "alias": alias})
            st.toast("Obra guardada"); st.rerun()

    st.subheader("Responsables de la obra")
    from core import usuarios as U
    from vistas.comun import rol as _rol
    todos = [u for u in U.listar(con) if u["activo"] and u["rol"] in ("jefe_obra", "tecnico", "direccion")]
    asignados = [u["id"] for u in todos if obra_id in U.obras_de(con, u["id"])]
    sel = st.multiselect("Jefes de obra y técnicos asignados", [u["id"] for u in todos], default=asignados, key=f"resp_{obra_id}",
                         format_func=lambda i: next(f"{u['nombre'] or u['usuario']} · {U.ROLES[u['rol']]}" for u in todos if u["id"] == i),
                         disabled=_rol() not in ("admin", "direccion", "gestor"),
                         help="Los jefes de obra solo ven sus obras y dan la conformidad a sus facturas antes de aprobarlas.")
    if sorted(sel) != sorted(asignados) and st.button("Guardar responsables"):
        U.asignar_obras(con, obra_id, sel, usuario()); st.toast("Responsables guardados"); st.rerun()
    if not any(u["rol"] == "jefe_obra" for u in todos if u["id"] in asignados):
        st.caption("Sin jefe de obra asignado: sus facturas no requieren conformidad de obra.")

    with st.popover("Eliminar esta obra", icon=":material/delete:"):
        st.caption("Solo si no tiene documentos ni certificaciones. Queda en «Papelera y deshacer» y se puede restaurar. "
                   "Para ocultarla sin borrar, desmarque «Activa».")
        if st.button("Confirmar eliminación", key=f"delobra_{obra_id}", type="primary"):
            from core import historial as H
            H.init(con)
            try:
                H.eliminar_obra(con, obra_id, usuario())
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    st.subheader("Partidas")
    part = pd.DataFrame(maestros.partidas_de_obra(con, obra_id))
    part["presupuesto_coste"] = part["presupuesto_coste_cents"].apply(lambda c: format(from_cents(c), "f").replace(".", ","))
    part["presupuesto_venta"] = part["presupuesto_venta_cents"].apply(lambda c: format(from_cents(c), "f").replace(".", ","))
    uso = {r["partida_id"]: r["n"] for r in db.rows(con, "SELECT partida_id, COUNT(*) n FROM lineas GROUP BY partida_id")}
    part["lineas"] = part["id"].map(uso).fillna(0).astype(int)
    ed = st.data_editor(part[["id", "codigo", "descripcion", "palabras_clave", "presupuesto_coste", "presupuesto_venta", "lineas"]],
                        hide_index=True, num_rows="dynamic", width="stretch", key=f"part_{obra_id}",
                        disabled=["id", "lineas"],
                        column_config={"palabras_clave": st.column_config.TextColumn("Palabras clave (reglas)", width="large"),
                                       "presupuesto_coste": "Ppto. coste (€)", "presupuesto_venta": "Ppto. venta (€)",
                                       "lineas": "Líneas imputadas"})
    tot = sum((parse_amount(x) for x in ed["presupuesto_coste"].fillna("") if str(x).strip()), D(0))
    st.caption(f"Σ presupuesto de coste de partidas: **{fmt_eur(tot)}**")
    if st.button("Guardar partidas", type="primary"):
        ids_orig = set(part["id"])
        ids_ed = set(ed["id"].dropna().astype(int))
        borrar = ids_orig - ids_ed
        con_lineas = [i for i in borrar if uso.get(i)]
        if con_lineas:
            st.error("No se pueden borrar partidas con líneas imputadas. Reasigna antes sus líneas.")
        else:
            with db.tx(con):
                for i in borrar:
                    con.execute("DELETE FROM partidas WHERE id=?", (int(i),))
                for r in ed.fillna("").to_dict("records"):
                    if not str(r["codigo"]).strip():
                        continue
                    vals = (str(r["codigo"]).strip(), r["descripcion"] or str(r["codigo"]), r["palabras_clave"] or "",
                            to_cents(parse_amount(str(r["presupuesto_coste"] or "0"))),
                            to_cents(parse_amount(str(r["presupuesto_venta"] or "0"))))
                    if r["id"] != "":
                        con.execute("UPDATE partidas SET codigo=?, descripcion=?, palabras_clave=?, presupuesto_coste_cents=?, "
                                    "presupuesto_venta_cents=? WHERE id=?", vals + (int(r["id"]),))
                    else:
                        con.execute("INSERT INTO partidas (codigo, descripcion, palabras_clave, presupuesto_coste_cents, "
                                    "presupuesto_venta_cents, obra_id) VALUES (?,?,?,?,?,?)", vals + (obra_id,))
                db.audit(con, usuario(), "editar_partidas", "obra", obra_id, {"borradas": list(map(int, borrar))})
            st.toast("Partidas guardadas"); st.rerun()

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Importar presupuesto (Excel / CSV)**")
        st.caption("Columnas: `codigo`, `descripcion`, `presupuesto_coste` y opcionalmente `presupuesto_venta`, "
                   "`palabras_clave`. Los códigos existentes se actualizan.")
        arch = st.file_uploader("Presupuesto", type=["xlsx", "xls", "csv"], label_visibility="collapsed")
        if arch and st.button("Importar"):
            n, a, av = _importar_presupuesto(con, obra_id, arch)
            if av:
                st.error("; ".join(av))
            else:
                st.success(f"{n} partidas nuevas · {a} actualizadas"); st.rerun()
        plantilla = pd.DataFrame([{"codigo": p, "descripcion": d, "presupuesto_coste": "0,00", "presupuesto_venta": "0,00",
                                   "palabras_clave": k} for p, d, k in maestros.PARTIDAS_ESTANDAR])
        st.download_button("Descargar plantilla CSV", plantilla.to_csv(index=False, sep=";").encode("utf-8-sig"),
                           "plantilla_presupuesto.csv", "text/csv")
    with c2:
        st.markdown("**Reclasificar líneas sin partida**")
        st.caption("Aplica las palabras clave a las líneas de esta obra sin partida o en «99 · Sin asignar». "
                   "No toca las asignadas a mano ni por la IA.")
        if st.button("Reclasificar por reglas"):
            partidas = maestros.partidas_de_obra(con, obra_id)
            p99 = maestros.partida_sin_asignar(con, obra_id)
            lin = db.rows(con, "SELECT l.id, l.descripcion, l.documento_id FROM lineas l JOIN documentos d ON d.id=l.documento_id "
                               "WHERE d.obra_id=? AND (l.partida_id IS NULL OR l.partida_id=?)", (obra_id, p99))
            n, docs = 0, set()
            with db.tx(con):
                for l in lin:
                    pid, conf = maestros.clasificar_linea(l["descripcion"] or "", partidas)
                    if pid:
                        con.execute("UPDATE lineas SET partida_id=?, partida_origen='reglas', partida_confianza=? WHERE id=?",
                                    (pid, conf, l["id"]))
                        n += 1; docs.add(l["documento_id"])
                db.audit(con, usuario(), "reclasificar_reglas", "obra", obra_id, {"lineas": n})
            for d in docs:
                validar_y_guardar(con, d)
            st.success(f"{n} de {len(lin)} líneas clasificadas.")



def _facturas_obra(con, obra_id: int, o: dict):
    """Todas las facturas y documentos de la obra, con filtros, totales, resumen por proveedor y por mes."""
    import pandas as pd
    import plotly.graph_objects as go
    from core.config import ESTADO_LABEL
    from vistas.comun import eur_col, ir_a_documento, puede_editar
    df = pd.DataFrame(db.rows(con, """
        SELECT d.id, d.fecha, COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, d.emisor_nif AS nif, d.numero, d.tipo_documento AS tipo,
               d.base_imponible_cents/100.0 AS base, d.total_factura_cents/100.0 AS total, d.total_a_pagar_cents/100.0 AS a_pagar,
               d.estado, d.pagada, d.conformado_por AS conformidad, d.cuenta_contable AS cuenta, d.filename AS archivo,
               (SELECT COUNT(*) FROM incidencias i WHERE i.documento_id=d.id AND i.resuelta=0 AND i.severidad IN ('critica','alta')) AS incid
        FROM documentos d LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
        WHERE d.obra_id=? ORDER BY d.fecha DESC, d.id DESC""", (obra_id,)))
    if df.empty:
        st.info("Esta obra aún no tiene facturas asignadas.")
        return
    estados = [e for e in ESTADO_LABEL if e in set(df["estado"])]
    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
    est = c1.multiselect("Estado", estados, default=[e for e in estados if e not in ("eliminado", "duplicado", "rechazada")],
                         format_func=ESTADO_LABEL.get, key=f"of_est_{obra_id}")
    prov = c2.text_input("Proveedor, nº o archivo", key=f"of_prov_{obra_id}")
    fechas = df["fecha"].dropna()
    desde = c3.date_input("Desde", value=pd.to_datetime(fechas.min()).date() if len(fechas) else None, format="DD/MM/YYYY",
                          key=f"of_desde_{obra_id}")
    hasta = c4.date_input("Hasta", value=pd.to_datetime(fechas.max()).date() if len(fechas) else None, format="DD/MM/YYYY",
                          key=f"of_hasta_{obra_id}")
    v = df[df["estado"].isin(est)] if est else df
    if prov:
        q = prov.lower()
        v = v[v.apply(lambda r: q in f"{r['proveedor']} {r['nif']} {r['numero']} {r['archivo']}".lower(), axis=1)]
    if desde:
        v = v[(v["fecha"].isna()) | (v["fecha"] >= desde.isoformat())]
    if hasta:
        v = v[(v["fecha"].isna()) | (v["fecha"] <= hasta.isoformat())]
    computables = v[v["tipo"].isin(["factura", "abono", "anticipo"])]
    k = st.columns(5)
    k[0].metric("Documentos", len(v))
    k[1].metric("Base imponible", f"{computables['base'].sum():,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))
    k[2].metric("Pendientes de revisión", int((v["estado"] == "pendiente_revision").sum()))
    k[3].metric("Aprobadas sin pagar", int(((v["estado"] == "aprobada") & (v["pagada"] == 0)).sum()))
    k[4].metric("Con incidencias graves", int((v["incid"] > 0).sum()))
    vis = v.assign(estado=v["estado"].map(ESTADO_LABEL), pagada=v["pagada"].astype(bool))
    st.dataframe(vis.drop(columns=["archivo"]), hide_index=True, width="stretch", height=min(520, 38 + 35 * len(vis)),
                 column_config={"base": eur_col("Base (€)"), "total": eur_col("Total (€)"), "a_pagar": eur_col("A pagar (€)"),
                                "pagada": st.column_config.CheckboxColumn("Pagada"), "incid": "Incid. graves",
                                "conformidad": "Conformidad", "cuenta": "Cuenta"})
    a, b, c = st.columns([3, 1, 1])
    if len(vis):
        sel = a.selectbox("Documento", vis["id"].tolist(), label_visibility="collapsed", key=f"of_sel_{obra_id}",
                          format_func=lambda i: f"#{i} · {vis.set_index('id').loc[i, 'proveedor'] or ''} · {vis.set_index('id').loc[i, 'numero'] or ''}")
        if puede_editar() and b.button("Abrir en revisión", icon=":material/open_in_new:", key=f"of_abrir_{obra_id}"):
            ir_a_documento(sel)
    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        vis.to_excel(xw, sheet_name="Facturas", index=False)
    c.download_button("Excel", buf.getvalue(), file_name=f"facturas_obra_{o['codigo']}.xlsx", icon=":material/download:",
                      mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if len(computables):
        s1, s2 = st.columns(2)
        with s1:
            st.markdown("**Por proveedor**")
            g = computables.groupby("proveedor", dropna=False).agg(facturas=("id", "count"), base=("base", "sum")) \
                .reset_index().sort_values("base", ascending=False)
            st.dataframe(g, hide_index=True, width="stretch", column_config={"base": eur_col("Base (€)")})
        with s2:
            st.markdown("**Por mes**")
            m = computables.dropna(subset=["fecha"]).assign(mes=lambda x: x["fecha"].str[:7]).groupby("mes")["base"].sum().reset_index()
            fig = go.Figure(go.Bar(x=m["mes"], y=m["base"], marker_color="#E1251B"))
            fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="€", separators=",.")
            st.plotly_chart(fig, width="stretch")
