"""Seguridad y Salud: CAE (empresas, trabajadores, documentación), control de acceso y trazabilidad de residuos."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import db, prevencion as PV
from vistas.comun import get_con, usuario, rol, selector_obra


def render():
    con = get_con()
    st.title("Seguridad y Salud")
    st.caption("Coordinación de actividades empresariales (CAE), control de acceso a obra y trazabilidad de residuos (albarán, factura y "
               "certificado del gestor).")
    obra_id = selector_obra("pv_obra", permitir_todas=False)
    if not obra_id:
        return
    edita = rol() in ("admin", "gestor", "direccion", "jefe_obra", "tecnico")
    t = st.tabs(["Control de acceso", "Empresas en obra", "Trabajadores", "Documentación", "Residuos"])
    with t[0]:
        _acceso(con, obra_id)
    with t[1]:
        _empresas(con, obra_id, edita)
    with t[2]:
        _trabajadores(con, obra_id, edita)
    with t[3]:
        _documentacion(con, edita)
    with t[4]:
        _residuos(con, obra_id, edita)


def _acceso(con, obra_id):
    st.caption("Consulta en la entrada de obra: con el DNI/NIE se comprueba empresa de alta, trabajador de alta y documentación vigente.")
    c = st.columns([2, 1, 1])
    dni = c[0].text_input("DNI / NIE", key="pv_dni")
    fecha = c[1].date_input("Fecha", date.today(), format="DD/MM/YYYY", key="pv_fac")
    reg = c[2].checkbox("Registrar la entrada", True, key="pv_reg")
    if dni:
        r = PV.puede_acceder(con, obra_id, dni, fecha, registrar=reg, usuario=usuario())
        w = r["trabajador"]
        if r["permitido"]:
            st.success(f"ACCESO PERMITIDO · {w['nombre']} ({w['empresa']})")
        else:
            st.error("ACCESO DENEGADO" + (f" · {w['nombre']} ({w['empresa']})" if w else ""))
            for m in r["motivos"]:
                st.markdown(f"- {m}")
    acc = db.rows(con, """SELECT a.fecha_hora, a.dni, w.nombre, a.permitido, a.motivos, a.usuario FROM cae_accesos a
                          LEFT JOIN cae_trabajadores w ON w.id=a.trabajador_id WHERE a.obra_id=? ORDER BY a.id DESC LIMIT 100""", (obra_id,))
    if acc:
        with st.expander("Últimos accesos registrados"):
            st.dataframe(pd.DataFrame(acc), hide_index=True, width="stretch")


def _empresas(con, obra_id, edita):
    es = PV.empresas_obra(con, obra_id, incluir_bajas=st.toggle("Ver también las dadas de baja", key="pv_bajas"))
    if es:
        df = pd.DataFrame(es)
        df["documentación"] = df.apply(lambda r: f"{r['docs_ok']}/{r['docs_total']}", axis=1)
        df["faltan"] = df["faltan"].map(lambda f: "; ".join(f))
        st.dataframe(df[["nombre", "nif", "nivel", "actividad", "fecha_alta", "fecha_baja", "trabajadores", "documentación", "faltan"]], hide_index=True,
                     width="stretch", column_config={"nombre": "Empresa", "nif": "NIF", "nivel": "Nivel", "actividad": "Actividad", "fecha_alta": "Alta",
                                                     "fecha_baja": "Baja", "trabajadores": "Trabajadores", "faltan": "Falta"})
    if not edita:
        return
    provs = db.rows(con, "SELECT id, nombre, nif FROM proveedores ORDER BY nombre")
    with st.form("pv_alta_emp", clear_on_submit=True):
        st.markdown("**Alta de empresa en la obra**")
        c = st.columns([3, 1, 2])
        p = c[0].selectbox("Empresa (proveedor)", [x["id"] for x in provs], format_func=lambda i: next(f"{x['nombre']} ({x['nif'] or 's/NIF'})" for x in provs if x["id"] == i))
        nivel = c[1].number_input("Nivel de subcontratación", 1, 5, 1)
        contr = c[2].selectbox("Contratada por", [None] + [x["proveedor_id"] for x in es], format_func=lambda i: "Llorca (contratista principal)" if i is None
                               else next(x["nombre"] for x in es if x["proveedor_id"] == i))
        c = st.columns(4)
        alta = c[0].date_input("Fecha de alta", date.today(), format="DD/MM/YYYY")
        act = c[1].text_input("Actividad")
        cont = c[2].text_input("Contacto PRL")
        mail = c[3].text_input("Correo")
        if st.form_submit_button("Dar de alta", type="primary"):
            try:
                PV.alta_empresa(con, obra_id, p, alta.isoformat(), usuario(), int(nivel), contr, act, cont, mail); st.rerun()
            except ValueError as e:
                st.error(str(e))
    vivas = [x for x in es if not x["fecha_baja"]]
    if vivas:
        c = st.columns([3, 1, 1, 1.4])
        sel = c[0].selectbox("Empresa", [x["id"] for x in vivas], format_func=lambda i: next(x["nombre"] for x in vivas if x["id"] == i), key="pv_emp_sel")
        fb = c[1].date_input("Fecha de baja", date.today(), format="DD/MM/YYYY", key="pv_fb")
        if c[2].button("Dar de baja"):
            PV.baja_empresa(con, sel, fb.isoformat(), usuario()); st.rerun()
        if c[3].button("Pedir documentación", help="Prepara el correo con lo que falta o caduca (bandeja de salida)."):
            try:
                PV.solicitar_documentacion(con, next(x["proveedor_id"] for x in vivas if x["id"] == sel), obra_id, usuario())
                st.success("Correo preparado en «Correo saliente».")
            except ValueError as e:
                st.error(str(e))


def _trabajadores(con, obra_id, edita):
    ts = PV.trabajadores_obra(con, obra_id)
    if ts:
        filas = []
        for w in ts:
            est = PV.estado_trabajador(con, w["id"])
            filas.append({"nombre": w["nombre"], "dni": w["dni"], "empresa": w["empresa"], "puesto": w["puesto"], "alta": w["fecha_alta"],
                          "documentación": f"{sum(1 for x in est if x['ok'])}/{len(est)}",
                          "falta": "; ".join(x["requisito"] for x in est if not x["ok"] and x["obligatorio"])})
        st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch")
    if not edita:
        return
    emps = PV.empresas_obra(con, obra_id)
    if not emps:
        st.info("Dé de alta antes a las empresas en la obra.")
        return
    with st.form("pv_alta_trab", clear_on_submit=True):
        st.markdown("**Alta de trabajador en la obra**")
        c = st.columns([2, 1.2, 2, 1.5, 1.2])
        nom = c[0].text_input("Nombre y apellidos")
        dni = c[1].text_input("DNI / NIE")
        emp = c[2].selectbox("Empresa", [x["proveedor_id"] for x in emps], format_func=lambda i: next(x["nombre"] for x in emps if x["proveedor_id"] == i))
        puesto = c[3].text_input("Puesto / categoría")
        alta = c[4].date_input("Alta", date.today(), format="DD/MM/YYYY")
        if st.form_submit_button("Dar de alta", type="primary"):
            try:
                tid = PV.alta_trabajador(con, emp, nom, dni, puesto, usuario())
                PV.alta_trabajador_obra(con, tid, obra_id, alta.isoformat(), usuario()); st.rerun()
            except ValueError as e:
                st.error(str(e))
    if ts:
        c = st.columns([3, 1, 1])
        sel = c[0].selectbox("Trabajador", [w["id"] for w in ts], format_func=lambda i: next(f"{w['nombre']} ({w['empresa']})" for w in ts if w["id"] == i),
                             key="pv_trab_sel")
        fb = c[1].date_input("Baja", date.today(), format="DD/MM/YYYY", key="pv_tfb")
        if c[2].button("Dar de baja en la obra"):
            PV.baja_trabajador_obra(con, sel, obra_id, fb.isoformat(), usuario()); st.rerun()


def _documentacion(con, edita):
    cad = PV.caducan_pronto(con, 15)
    if cad:
        st.warning(f"{len(cad)} documento(s) caducados o que caducan en 15 días.")
        st.dataframe(pd.DataFrame(cad)[["requisito", "empresa", "trabajador", "fecha_caducidad"]], hide_index=True, width="stretch")
    pend = db.rows(con, """SELECT d.*, r.nombre AS requisito, COALESCE(p.nombre, pw.nombre) AS empresa, w.nombre AS trabajador FROM cae_documentos d
                           JOIN cae_requisitos r ON r.id=d.requisito_id LEFT JOIN proveedores p ON p.id=d.proveedor_id
                           LEFT JOIN cae_trabajadores w ON w.id=d.trabajador_id LEFT JOIN proveedores pw ON pw.id=w.proveedor_id
                           WHERE d.estado='pendiente_validar' ORDER BY d.id""")
    if pend:
        st.markdown(f"**{len(pend)} documento(s) pendientes de validar**")
        for d in pend:
            with st.container(border=True):
                a, b = st.columns([4, 1.3])
                a.markdown(f"**{d['requisito']}** · {d['empresa'] or ''}{' · ' + d['trabajador'] if d['trabajador'] else ''}  \n"
                           f":gray[Emitido {d['fecha_emision'] or '—'} · caduca {d['fecha_caducidad'] or 'no caduca'} · subido por {d['subido_por']}]")
                if d["archivo"]:
                    from pathlib import Path
                    if Path(d["archivo"]).exists():
                        b.download_button("Ver", Path(d["archivo"]).read_bytes(), file_name=Path(d["archivo"]).name, key=f"pv_dv_{d['id']}")
                if edita:
                    if b.button("Validar", key=f"pv_ok_{d['id']}", type="primary"):
                        try:
                            PV.validar_documento(con, d["id"], usuario(), True); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                    mot = a.text_input("Motivo del rechazo", key=f"pv_mr_{d['id']}")
                    if b.button("Rechazar", key=f"pv_no_{d['id']}"):
                        try:
                            PV.validar_documento(con, d["id"], usuario(), False, mot); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
    if not edita:
        return
    st.markdown("**Subir documento**")
    reqs = PV.requisitos(con)
    c = st.columns([3, 3])
    req = c[0].selectbox("Documento", [r["id"] for r in reqs], format_func=lambda i: next(f"{'Empresa' if r['ambito'] == 'empresa' else 'Trabajador'}: {r['nombre']}"
                                                                                          for r in reqs if r["id"] == i), key="pv_req")
    ambito = next(r["ambito"] for r in reqs if r["id"] == req)
    if ambito == "empresa":
        ps = db.rows(con, "SELECT DISTINCT p.id, p.nombre FROM cae_empresas_obra e JOIN proveedores p ON p.id=e.proveedor_id ORDER BY p.nombre")
        quien = c[1].selectbox("Empresa", [p["id"] for p in ps], format_func=lambda i: next(p["nombre"] for p in ps if p["id"] == i), key="pv_q_e") if ps else None
    else:
        ws = db.rows(con, "SELECT w.id, w.nombre, p.nombre AS emp FROM cae_trabajadores w JOIN proveedores p ON p.id=w.proveedor_id WHERE w.activo=1 ORDER BY w.nombre")
        quien = c[1].selectbox("Trabajador", [w["id"] for w in ws], format_func=lambda i: next(f"{w['nombre']} ({w['emp']})" for w in ws if w["id"] == i),
                               key="pv_q_t") if ws else None
    c = st.columns(3)
    fe = c[0].date_input("Fecha de emisión", date.today(), format="DD/MM/YYYY", key="pv_fe")
    fc = c[1].date_input("Caducidad (vacío: según el tipo)", None, format="DD/MM/YYYY", key="pv_fc")
    up = c[2].file_uploader("Archivo", key="pv_up")
    if st.button("Subir documento", disabled=not quien):
        try:
            kw = {"proveedor_id": quien} if ambito == "empresa" else {"trabajador_id": quien}
            PV.subir_documento(con, req, usuario(), fecha_emision=fe.isoformat(), fecha_caducidad=fc.isoformat() if fc else None,
                               nombre=up.name if up else "", data=up.getvalue() if up else None, **kw)
            st.success("Subido: queda pendiente de validar."); st.rerun()
        except ValueError as e:
            st.error(str(e))
    if rol() == "admin":
        with st.expander("Catálogo de documentos exigidos"):
            ed = st.data_editor(pd.DataFrame(PV.requisitos(con, solo_activos=False)), hide_index=True, width="stretch", key="pv_cat",
                                disabled=["id", "ambito", "nombre"], column_config={"id": None, "ambito": "Ámbito", "nombre": "Documento",
                                                                                     "meses_validez": "Validez (meses)", "obligatorio": "Obligatorio",
                                                                                     "bloquea_acceso": "Bloquea acceso", "activo": "Activo"})
            nuevo = st.text_input("Añadir documento exigido", key="pv_cat_n")
            amb = st.radio("Ámbito", ["empresa", "trabajador"], horizontal=True, key="pv_cat_a")
            if st.button("Guardar catálogo"):
                with db.tx(con):
                    for r in ed.to_dict("records"):
                        con.execute("UPDATE cae_requisitos SET meses_validez=?, obligatorio=?, bloquea_acceso=?, activo=? WHERE id=?",
                                    (None if pd.isna(r["meses_validez"]) else int(r["meses_validez"]), int(bool(r["obligatorio"])),
                                     int(bool(r["bloquea_acceso"])), int(bool(r["activo"])), r["id"]))
                    if nuevo.strip():
                        con.execute("INSERT OR IGNORE INTO cae_requisitos (ambito, nombre) VALUES (?,?)", (amb, nuevo.strip()))
                    db.audit(con, usuario(), "cae_catalogo", "ajustes", None, None)
                st.rerun()


def _residuos(con, obra_id, edita):
    PV.enlazar_facturas(con, obra_id)
    ret = PV.retiradas(con, obra_id)
    res = PV.resumen_ler(con, obra_id)
    if res:
        d = pd.DataFrame(res)
        d["real"] = d["real"].map(float)
        d["previsto"] = d["previsto"].map(lambda v: float(v) if v is not None else None)
        st.dataframe(d, hide_index=True, width="stretch", column_config={"ler": "LER", "descripcion": "Residuo", "unidad": "Ud", "real": "Retirado",
                                                                          "previsto": "Previsto (EGR)", "desviacion_pct": st.column_config.NumberColumn("Desviación", format="%.1f %%")})
    if ret:
        incompletas = [r for r in ret if r["trazabilidad"] != "Completa"]
        if incompletas:
            st.warning(f"{len(incompletas)} retirada(s) sin trazabilidad completa.")
        st.dataframe(pd.DataFrame(ret)[["fecha", "ler", "descripcion", "cantidad", "unidad", "gestor", "albaran", "factura_numero", "certificado", "trazabilidad"]],
                     hide_index=True, width="stretch",
                     column_config={"fecha": "Fecha", "ler": "LER", "descripcion": "Residuo", "cantidad": "Cantidad", "unidad": "Ud", "gestor": "Gestor",
                                    "albaran": "Albarán", "factura_numero": "Factura", "certificado": "Certificado", "trazabilidad": "Trazabilidad"})
    if not edita:
        return
    with st.form("pv_res", clear_on_submit=True):
        st.markdown("**Nueva retirada**")
        c = st.columns([1.2, 2.5, 1, 0.8])
        fecha = c[0].date_input("Fecha", date.today(), format="DD/MM/YYYY")
        ler = c[1].selectbox("Código LER", list(PV.LER_FRECUENTES) + ["otro"], format_func=lambda k: k if k == "otro" else f"{k} · {PV.LER_FRECUENTES[k]}")
        cant = c[2].text_input("Cantidad")
        ud = c[3].selectbox("Ud", ["t", "m3", "kg", "ud"])
        otro = st.text_input("Código LER (si «otro»)")
        c = st.columns(4)
        trans = c[0].text_input("Transportista")
        gestor = c[1].text_input("Gestor autorizado")
        nima = c[2].text_input("NIMA del gestor")
        alb = c[3].text_input("Nº de albarán / DI")
        c = st.columns(2)
        f_alb = c[0].file_uploader("Albarán / documento de identificación", key="pv_ralb")
        f_cer = c[1].file_uploader("Certificado del gestor (si ya lo hay)", key="pv_rcer")
        cer = st.text_input("Nº del certificado")
        if st.form_submit_button("Registrar retirada", type="primary"):
            try:
                PV.registrar_retirada(con, obra_id, {"fecha": fecha.isoformat(), "ler": otro if ler == "otro" else ler, "cantidad": cant, "unidad": ud,
                                                     "transportista": trans, "gestor": gestor, "gestor_nima": nima, "albaran": alb, "certificado": cer or None},
                                      usuario(), (f_alb.name, f_alb.getvalue()) if f_alb else None, (f_cer.name, f_cer.getvalue()) if f_cer else None)
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    pend = [r for r in ret if r["trazabilidad"] != "Completa"]
    if pend:
        with st.expander("Completar una retirada (certificado o factura)"):
            sel = st.selectbox("Retirada", [r["id"] for r in pend], format_func=lambda i: next(f"{r['fecha']} · {r['ler']} · {r['cantidad']} {r['unidad']} · "
                                                                                                f"{r['trazabilidad']}" for r in pend if r["id"] == i))
            cer = st.text_input("Nº de certificado", key="pv_cc_n")
            fcer = st.file_uploader("Certificado", key="pv_cc_f")
            facts = db.rows(con, "SELECT id, numero, emisor_nombre FROM documentos WHERE obra_id=? AND tipo_documento='factura' ORDER BY fecha DESC LIMIT 300", (obra_id,))
            fac = st.selectbox("Factura del gestor", [None] + [f["id"] for f in facts], format_func=lambda i: "—" if i is None else
                               next(f"{f['emisor_nombre']} · {f['numero']}" for f in facts if f["id"] == i))
            if st.button("Guardar"):
                PV.completar_retirada(con, sel, usuario(), cer or None, date.today().isoformat() if (cer or fcer) else None,
                                      (fcer.name, fcer.getvalue()) if fcer else None, fac); st.rerun()
    with st.expander("Cantidades previstas en el estudio de gestión de residuos"):
        prev = pd.DataFrame(db.rows(con, "SELECT ler, descripcion, cantidad, unidad FROM residuos_previstos WHERE obra_id=?", (obra_id,)))
        if prev.empty:
            prev = pd.DataFrame(columns=["ler", "descripcion", "cantidad", "unidad"])
        ed = st.data_editor(prev, num_rows="dynamic", hide_index=True, width="stretch", key="pv_prev")
        if st.button("Guardar previstos"):
            PV.guardar_previstos(con, obra_id, ed.fillna("").to_dict("records"), usuario()); st.rerun()
