"""Costes y ventas internos: lo que no llega en factura de proveedor pero es coste o venta de la obra. Solo uso interno."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import db, internos as I, maestros
from core.money import parse_amount, fmt_eur
from vistas.comun import get_con, usuario, rol, selector_obra, eur_col, obras_permitidas


def _eur(v) -> str:
    return fmt_eur(v)


def render():
    con = get_con()
    I.init(con)
    st.title("Costes y ventas internos")
    st.caption("Mano de obra propia, personal técnico, maquinaria y medios propios, instalaciones, seguros, tasas, consumos, gastos "
               "generales imputados, cargos a subcontratas y ventas aún no certificadas. Cuentan en la rentabilidad y en los "
               "informes internos; **nunca** salen en documentos para el cliente. Cada movimiento lleva su justificante adjunto.")
    obra_id = selector_obra("int_obra", permitir_todas=False)
    if not obra_id:
        return
    puede_crear = rol() in ("admin", "direccion", "gestor", "jefe_obra")
    puede_validar = rol() in ("admin", "direccion", "gestor")
    hoy = date.today()
    mes = I.resumen(con, obra_id, hoy.replace(day=1).isoformat(), hoy.isoformat())
    ori = I.resumen(con, obra_id)
    k = st.columns(5)
    k[0].metric("Coste interno del mes", _eur(mes["coste"] + mes["mano_obra"]))
    k[1].metric("  · de ello, mano de obra", _eur(mes["mano_obra"]))
    k[2].metric("Cargos a subcontratas (mes)", _eur(mes["cargos"]))
    k[3].metric("Ventas internas (mes)", _eur(mes["venta"]))
    k[4].metric("Pendientes de validar", ori["borradores"])
    st.caption(f"A origen: coste interno neto {_eur(ori['coste_neto'])} · ventas internas {_eur(ori['venta'])} · {ori['n']} movimiento(s).")

    tabs = st.tabs(["Registrar", "Movimientos", "Importar partes de trabajo", "Repartir gastos generales", "Tarifas de mano de obra"])
    partidas = maestros.partidas_de_obra(con, obra_id)
    labp = {p["id"]: f"{p['codigo']} · {p['descripcion'][:40]}" for p in partidas}
    oficios = db.rows(con, "SELECT id, nombre FROM aud_categorias WHERE obra_id=? ORDER BY orden, nombre", (obra_id,)) \
        if db.one(con, "SELECT name FROM sqlite_master WHERE name='aud_categorias'") else []
    labo = {o["id"]: o["nombre"] for o in oficios}

    # ---------------------------------------------------------------- registrar
    with tabs[0]:
        if not puede_crear:
            st.caption("Su rol no puede registrar movimientos internos.")
        else:
            c1, c2, c3 = st.columns([1.2, 2, 1])
            tipo = c1.selectbox("Tipo", list(I.TIPOS), format_func=I.TIPOS.get, key="int_tipo")
            cat = c2.selectbox("Categoría", I.CATEGORIAS[tipo], key=f"int_cat_{tipo}")
            fecha = c3.date_input("Fecha", hoy, format="DD/MM/YYYY", key="int_fecha")
            es_mo = cat in I.ES_MANO_OBRA
            with st.form("int_nuevo", clear_on_submit=True):
                desc = st.text_input("Descripción *", placeholder="p. ej. Cuadrilla de albañilería semana 38 · Alquiler caseta septiembre · "
                                                                  "Repercusión limpieza a Pinturas X")
                importe_txt, horas_txt, tarifa_sel, ph_txt = "", "", None, ""
                if es_mo:
                    a, b, c = st.columns(3)
                    horas_txt = a.text_input("Horas", placeholder="40")
                    tar = I.tarifas(con)
                    tarifa_sel = b.selectbox("Categoría profesional", list(tar), format_func=lambda x: f"{x} · {tar[x]} €/h")
                    ph_txt = c.text_input("Coste/hora (si distinto de la tarifa)", placeholder=str(tar.get(tarifa_sel, "")))
                else:
                    importe_txt = st.text_input("Importe (€) *", placeholder="1250,00")
                a, b = st.columns(2)
                pid = a.selectbox("Capítulo (opcional)", [None] + list(labp), format_func=lambda x: "— sin capítulo —" if x is None else labp[x])
                oid = b.selectbox("Oficio (opcional)", [None] + list(labo), format_func=lambda x: "— sin oficio —" if x is None else labo[x])
                prov_id = None
                if tipo == "cargo":
                    provs = db.rows(con, "SELECT id, nombre FROM proveedores ORDER BY nombre")
                    prov_id = st.selectbox("Subcontrata a la que se repercute", [None] + [p["id"] for p in provs],
                                           format_func=lambda x: "— elegir —" if x is None else next(p["nombre"] for p in provs if p["id"] == x))
                notas = st.text_area("Notas", height=68)
                archivos = st.file_uploader("Justificantes (partes, nóminas, contratos, hojas de cálculo, fotos…)",
                                            type=I.EXT_ADJ, accept_multiple_files=True)
                if st.form_submit_button("Guardar movimiento", type="primary"):
                    try:
                        cantidad = precio = None
                        if es_mo:
                            horas = parse_amount(horas_txt or "0")
                            precio = parse_amount(ph_txt) if ph_txt.strip() else I.tarifas(con)[tarifa_sel]
                            if horas <= 0:
                                raise ValueError("Indique las horas.")
                            importe, cantidad = horas * precio, horas
                        else:
                            if not importe_txt.strip() or not __import__("re").fullmatch(r"-?[\d.,\s]+€?", importe_txt.strip()):
                                raise ValueError("El importe no es un número válido.")
                            importe = parse_amount(importe_txt)
                        if tipo == "cargo" and not prov_id:
                            raise ValueError("Indique la subcontrata a la que se repercute el cargo.")
                        if db.get_setting(con, "internos_adjunto_obligatorio", "1") == "1" and not archivos:
                            st.warning("Se guarda como borrador, pero no se podrá validar hasta adjuntar el justificante.")
                        mid = I.crear(con, obra_id, tipo, cat, fecha.isoformat(), desc, importe, usuario(), pid, oid, prov_id,
                                      cantidad, "h" if es_mo else None, precio, notas or None)
                        for f in archivos or []:
                            I.adjuntar(con, mid, f.name, f.getvalue(), usuario())
                        st.success(f"Movimiento #{mid} guardado ({_eur(importe)}), pendiente de validar.")
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))

    # ---------------------------------------------------------------- movimientos
    with tabs[1]:
        c1, c2, c3 = st.columns(3)
        ver_anulados = c1.toggle("Ver anulados", key="int_ver_anul")
        f_tipo = c2.multiselect("Tipo", list(I.TIPOS), format_func=I.TIPOS.get, key="int_f_tipo")
        f_est = c3.multiselect("Estado", list(I.ESTADOS), format_func=I.ESTADOS.get, key="int_f_est")
        q = """SELECT m.*, p.codigo AS capitulo, pr.nombre AS proveedor, (SELECT COUNT(*) FROM mov_adjuntos a WHERE a.movimiento_id=m.id) AS adj
               FROM mov_internos m LEFT JOIN partidas p ON p.id=m.partida_id LEFT JOIN proveedores pr ON pr.id=m.proveedor_id
               WHERE m.obra_id=?""" + ("" if ver_anulados else " AND m.estado<>'anulado'") + " ORDER BY m.fecha DESC, m.id DESC"
        df = pd.DataFrame(db.rows(con, q, (obra_id,)))
        if not df.empty and f_tipo:
            df = df[df["tipo"].isin(f_tipo)]
        if not df.empty and f_est:
            df = df[df["estado"].isin(f_est)]
        if df.empty:
            st.caption("Sin movimientos.")
        else:
            vis = df.assign(importe=df.apply(lambda r: r["importe_cents"] / 100 * (-1 if r["tipo"] == "cargo" else 1), axis=1),
                            tipo=df["tipo"].map(I.TIPOS), estado=df["estado"].map(I.ESTADOS),
                            oficio=df["oficio_id"].map(lambda x: labo.get(x) if x == x and x is not None else None))
            st.dataframe(vis[["id", "fecha", "tipo", "categoria", "descripcion", "capitulo", "oficio", "proveedor", "cantidad", "unidad",
                              "importe", "adj", "estado", "creado_por", "validado_por"]], hide_index=True, width="stretch",
                         column_config={"importe": eur_col("Importe (€)", "Los cargos a subcontratas aparecen en negativo: restan coste"),
                                        "adj": st.column_config.NumberColumn("Adjuntos"), "descripcion": st.column_config.TextColumn(width="large")})
            sel = st.selectbox("Movimiento", df["id"].tolist(), key="int_sel",
                               format_func=lambda i: f"#{i} · {df.set_index('id').loc[i, 'descripcion'][:70]}")
            m = df.set_index("id").loc[sel]
            a, b, c, d = st.columns(4)
            if m["estado"] == "borrador" and puede_validar and a.button("Validar", type="primary", key="int_val"):
                ok, msg = I.validar(con, int(sel), usuario(), rol())
                (st.toast if ok else st.error)(msg)
                if ok:
                    st.rerun()
            if m["estado"] != "anulado" and (puede_validar or m["creado_por"] == usuario()):
                with b.popover("Anular"):
                    motivo = st.text_input("Motivo", key=f"int_mot_{sel}")
                    if st.button("Confirmar anulación", key=f"int_anu_{sel}"):
                        try:
                            I.anular(con, int(sel), usuario(), motivo); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
            if m["estado"] == "anulado" and puede_validar and c.button("Reactivar", key="int_react"):
                I.reactivar(con, int(sel), usuario()); st.rerun()
            with d.popover("Añadir justificante"):
                nuevos = st.file_uploader("Archivos", type=I.EXT_ADJ, accept_multiple_files=True, key=f"int_add_{sel}")
                if nuevos and st.button("Subir", key=f"int_sub_{sel}"):
                    for f in nuevos:
                        I.adjuntar(con, int(sel), f.name, f.getvalue(), usuario())
                    st.rerun()
            adj = I.adjuntos(con, int(sel))
            if adj:
                st.markdown("**Justificantes**")
                for x in adj:
                    try:
                        st.download_button(f"{x['nombre']} · {x['bytes'] / 1024:,.0f} KB · {x['subido_por']}", open(x["ruta"], "rb").read(),
                                           file_name=x["nombre"], key=f"int_dl_{x['id']}")
                    except FileNotFoundError:
                        st.error(f"Falta el archivo en disco: {x['nombre']}")
            if m["notas"]:
                st.caption(f"Notas: {m['notas'][:600]}")
            if m["estado"] == "anulado":
                st.caption(f"Anulado por {m['anulado_por']} ({m['anulado_en']}): {m['motivo_anulacion']}")

    # ---------------------------------------------------------------- importar partes
    with tabs[2]:
        st.caption("Excel o CSV con columnas: **fecha**, **horas** y, opcionalmente, trabajador, categoría, coste/hora, obra (código), "
                   "capítulo y descripción. Sin coste/hora se usa la tarifa de la categoría. Se agrupa por obra, capítulo y día, "
                   "y el archivo queda adjunto como justificante.")
        f = st.file_uploader("Partes de trabajo", type=["xlsx", "xls", "csv"], key="int_partes")
        if f and puede_crear and st.button("Importar partes", type="primary"):
            try:
                r = I.importar_partes(con, f.getvalue(), f.name, obra_id, usuario())
                st.success(f"{r['movimientos']} movimiento(s) de mano de obra creados a partir de {r['filas']} fila(s).")
                for e in r["errores"][:30]:
                    st.warning(e)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))

    # ---------------------------------------------------------------- repartir gastos generales
    with tabs[3]:
        if not puede_validar:
            st.caption("Solo Administración o Dirección.")
        else:
            st.caption("Imputa un gasto general del mes (oficina, estructura, seguros globales…) a varias obras. Queda como borrador en "
                       "cada obra con el cálculo del reparto explicado.")
            with st.form("int_gg"):
                c1, c2, c3 = st.columns(3)
                imp = c1.text_input("Importe total (€)")
                periodo = c2.text_input("Mes (AAAA-MM)", hoy.strftime("%Y-%m"))
                criterio = c3.selectbox("Criterio", ["venta_mes", "coste_mes", "igual"],
                                        format_func={"venta_mes": "Proporcional a la venta certificada del mes",
                                                     "coste_mes": "Proporcional al coste facturado del mes",
                                                     "igual": "A partes iguales"}.get)
                todas = maestros.listar_obras(con, solo_activas=True)
                perm = obras_permitidas()
                if perm is not None:
                    todas = [o for o in todas if o["id"] in perm]
                obras_sel = st.multiselect("Obras", [o["id"] for o in todas], default=[o["id"] for o in todas],
                                           format_func=lambda i: next(f"{o['codigo']} · {o['nombre']}" for o in todas if o["id"] == i))
                desc = st.text_input("Descripción", "Gastos generales de estructura imputados")
                if st.form_submit_button("Repartir", type="primary"):
                    try:
                        import re as _re
                        if not _re.fullmatch(r"\d{4}-\d{2}", periodo.strip()):
                            raise ValueError("Mes con formato AAAA-MM.")
                        out = I.repartir_gastos_generales(con, parse_amount(imp or "0"), periodo.strip(), criterio, obras_sel, desc, usuario())
                        st.success("Reparto creado: " + ", ".join(f"#{m} {_eur(v)}" for m, v in out))
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))

    # ---------------------------------------------------------------- tarifas
    with tabs[4]:
        tar = pd.DataFrame([{"categoria": k_, "coste_hora": float(v)} for k_, v in I.tarifas(con).items()])
        ed = st.data_editor(tar, num_rows="dynamic", hide_index=True, key="int_tar", disabled=not puede_validar,
                            column_config={"categoria": "Categoría profesional",
                                           "coste_hora": st.column_config.NumberColumn("Coste empresa €/h", format="%.2f", min_value=0)})
        st.caption("Coste empresa por hora (salario, Seguridad Social y costes asociados). Se usa al registrar o importar mano de obra.")
        if puede_validar and st.button("Guardar tarifas"):
            with db.tx(con):
                con.execute("DELETE FROM tarifas_mo")
                for r in ed.dropna(subset=["categoria"]).to_dict("records"):
                    if str(r["categoria"]).strip() and r["coste_hora"] and r["coste_hora"] > 0:
                        con.execute("INSERT INTO tarifas_mo (categoria, coste_hora, actualizado_por, actualizado_en) VALUES (?,?,?,?)",
                                    (str(r["categoria"]).strip(), f"{r['coste_hora']:.2f}", usuario(), db.now_iso()))
                db.audit(con, usuario(), "tarifas_mano_obra", "ajustes", None, ed.to_dict("records"))
            st.toast("Tarifas guardadas"); st.rerun()
