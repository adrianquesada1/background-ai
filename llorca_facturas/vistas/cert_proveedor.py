"""Certificación a subcontratas: líneas de contrato, medición mensual, aprobación y autorización de facturación."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

from core import db, cert_proveedor as CP
from core.money import fmt_eur
from vistas.comun import get_con, usuario, rol, selector_obra, eur_col


def _e(c):
    return fmt_eur(Decimal(c or 0) / 100)


def render():
    con = get_con()
    st.title("Certificación a subcontratas")
    st.caption("La obra mide lo ejecutado por cada subcontrata, Administración lo aprueba y el subcontratista recibe la autorización "
               "para facturar exactamente lo aprobado. Su factura casa sola con la certificación cuando llega.")
    obra_id = selector_obra("cp_obra", permitir_todas=False)
    if not obra_id:
        return
    cons = CP.contratos(con, obra_id)
    if not cons:
        st.info("La obra no tiene contratos: adjudique las ofertas en «Contratación».")
        return
    df = pd.DataFrame(cons)
    df["contratado"] = df["importe_cents"] / 100
    df["certificado"] = df["certificado_origen_cents"] / 100
    df["pct"] = (df["certificado_origen_cents"] / df["importe_cents"].where(df["importe_cents"] != 0) * 100).round(1)
    st.dataframe(df[["proveedor", "alcance", "contratado", "certificado", "pct", "ultima_cert", "n_lineas"]], hide_index=True, width="stretch",
                 column_config={"proveedor": "Subcontrata", "alcance": "Alcance", "contratado": eur_col("Contratado (€)"),
                                "certificado": eur_col("Certificado a origen (€)"), "pct": st.column_config.NumberColumn("% certificado", format="%.1f %%"),
                                "ultima_cert": "Última cert.", "n_lineas": "Líneas"})
    of = st.selectbox("Contrato", [c["oferta_id"] for c in cons], key="cp_of",
                      format_func=lambda i: next(f"{c['proveedor']} · {c['alcance'] or ''} · {_e(c['importe_cents'])}" for c in cons if c["oferta_id"] == i))
    t1, t2, t3 = st.tabs(["Certificar", "Líneas y condiciones del contrato", "Certificaciones emitidas"])
    with t2:
        _lineas(con, of)
    with t1:
        _certificar(con, of)
    with t3:
        _listado(con, obra_id)


def _lineas(con, of):
    edita = rol() in ("admin", "gestor", "direccion", "tecnico", "jefe_obra")
    cond = CP.condiciones(con, of)
    with st.form(f"cp_cond_{of}"):
        c = st.columns(4)
        ret = c[0].text_input("Retención de garantía (%)", cond["ret_pct"])
        isp = c[1].checkbox("Inversión del sujeto pasivo", bool(cond["isp"]))
        iva = c[2].text_input("IVA si no hay ISP (%)", cond["iva_pct"])
        email = c[3].text_input("Correo del subcontratista", cond.get("email") or "")
        fp = st.text_input("Forma de pago", cond.get("forma_pago") or "")
        if st.form_submit_button("Guardar condiciones", disabled=not edita):
            try:
                CP.guardar_condiciones(con, of, {"ret_pct": ret, "isp": isp, "iva_pct": iva, "email": email, "forma_pago": fp}, usuario())
                st.success("Guardado.")
            except ValueError as e:
                st.error(str(e))
    ls = CP.lineas(con, of)
    base = pd.DataFrame(ls)[["id", "codigo", "descripcion", "unidad", "cantidad", "precio", "cert_partida"]] if ls else \
        pd.DataFrame(columns=["id", "codigo", "descripcion", "unidad", "cantidad", "precio", "cert_partida"])
    st.caption("Cantidad y precio del contrato. «Partida cliente»: código de la partida de la certificación al cliente a la que corresponde "
               "(sirve para preparar la certificación al cliente a partir de lo medido a las subcontratas).")
    up = st.file_uploader("Cargar líneas desde Excel/CSV (código, descripción, unidad, cantidad, precio, partida cliente)", type=["xlsx", "xls", "csv"],
                          key=f"cp_up_{of}")
    if up is not None:
        try:
            base = pd.DataFrame(CP.leer_excel_lineas(up.getvalue(), up.name))
            st.info(f"{len(base)} línea(s) leídas: revíselas y pulse «Guardar líneas».")
        except ValueError as e:
            st.error(str(e))
    sols = db.rows(con, """SELECT s.id, s.empresa, e.nombre FROM estudio_solicitudes s JOIN estudios e ON e.id=s.estudio_id
                           WHERE s.estado='recibida' ORDER BY s.id DESC LIMIT 200""")
    if sols:
        c1, c2 = st.columns([3, 1])
        s = c1.selectbox("…o tomar las líneas de una oferta del estudio", [None] + [x["id"] for x in sols], key=f"cp_est_{of}",
                         format_func=lambda i: "—" if i is None else next(f"{x['nombre']} · {x['empresa']}" for x in sols if x["id"] == i))
        if s and c2.button("Usar esta oferta", key=f"cp_est_b_{of}"):
            base = pd.DataFrame(CP.lineas_desde_estudio(con, s))
    ed = st.data_editor(base, num_rows="dynamic", hide_index=True, width="stretch", key=f"cp_ed_{of}", disabled=["id"] if edita else True,
                        column_config={"id": None, "codigo": "Código", "descripcion": st.column_config.TextColumn("Descripción", width="large"),
                                       "unidad": "Ud", "cantidad": "Cantidad contrato", "precio": "Precio (€)", "cert_partida": "Partida cliente"})
    if edita and st.button("Guardar líneas", type="primary", key=f"cp_gl_{of}"):
        try:
            r = CP.guardar_lineas(con, of, ed.fillna("").to_dict("records"), usuario())
            msg = f"{r['lineas']} línea(s) · total {_e(r['total_cents'])}"
            if r["diferencia_cents"]:
                st.warning(msg + f". No cuadra con el importe adjudicado ({_e(r['oferta_cents'])}): diferencia {_e(r['diferencia_cents'])}.")
            else:
                st.success(msg + ": cuadra con el importe adjudicado.")
        except ValueError as e:
            st.error(str(e))


def _certificar(con, of):
    bor = db.one(con, "SELECT * FROM certs_proveedor WHERE oferta_id=? AND estado='borrador'", (of,))
    if not bor:
        c = st.columns([1, 1, 2])
        hoy = date.today()
        periodo = c[0].text_input("Periodo (AAAA-MM)", hoy.strftime("%Y-%m"), key=f"cp_per_{of}")
        fecha = c[1].date_input("Fecha", hoy, format="DD/MM/YYYY", key=f"cp_fe_{of}")
        if c[2].button("Nueva certificación", type="primary", disabled=rol() not in ("admin", "gestor", "direccion", "jefe_obra", "tecnico")):
            try:
                CP.nueva(con, of, periodo.strip(), fecha.isoformat(), usuario()); st.rerun()
            except ValueError as e:
                st.error(str(e))
        return
    st.markdown(f"**Certificación nº {bor['numero']} · {bor['periodo']}** · en medición · medida por {bor['medido_por']}")
    det = CP.detalle(con, bor["id"])
    df = pd.DataFrame(det)
    vis = pd.DataFrame({"linea_id": df["linea_id"], "codigo": df["codigo"], "descripcion": df["descripcion"], "unidad": df["unidad"],
                        "contrato": df["cant_contrato"].map(float), "anterior": df["cant_anterior"].map(float),
                        "origen": df["cant_origen"].map(float), "precio": df["precio"].map(float),
                        "mes_eur": df["importe_mes_cents"] / 100, "obs": df["observaciones"].fillna("")})
    a_origen = st.radio("Medición", ["A origen", "Del mes"], horizontal=True, key=f"cp_modo_{bor['id']}") == "A origen"
    if not a_origen:
        vis["origen"] = vis["origen"] - vis["anterior"]
    ed = st.data_editor(vis, hide_index=True, width="stretch", key=f"cp_med_{bor['id']}",
                        disabled=["linea_id", "codigo", "descripcion", "unidad", "contrato", "anterior", "precio", "mes_eur"],
                        column_config={"linea_id": None, "codigo": "Código", "descripcion": st.column_config.TextColumn("Descripción", width="large"),
                                       "unidad": "Ud", "contrato": "Contrato", "anterior": "Anterior",
                                       "origen": st.column_config.NumberColumn("A origen" if a_origen else "Del mes", format="%.3f"),
                                       "precio": "Precio", "mes_eur": eur_col("Importe mes (€)"), "obs": "Observaciones"})
    if st.button("Guardar medición", key=f"cp_gm_{bor['id']}"):
        try:
            av = CP.medir(con, bor["id"], {int(r["linea_id"]): str(r["origen"]) for r in ed.to_dict("records")}, usuario(), a_origen,
                          {int(r["linea_id"]): r["obs"] or None for r in ed.to_dict("records")})
            if not av:
                st.rerun()
            for x in av:
                st.warning(x)
        except ValueError as e:
            st.error(str(e))
    c = db.one(con, "SELECT * FROM certs_proveedor WHERE id=?", (bor["id"],))
    k = st.columns(5)
    k[0].metric("Base del mes", _e(c["base_mes_cents"]))
    k[1].metric("IVA", "ISP" if c["isp"] else _e(c["iva_cents"]))
    k[2].metric(f"Retención {c['ret_pct']} %", _e(c["ret_cents"]))
    k[3].metric("Líquido", _e(c["liquido_cents"]))
    k[4].metric("A origen", _e(c["base_origen_cents"]))
    avisos = CP.avisos(con, bor["id"])
    for x in avisos:
        st.warning(x)
    oc = st.text_input("Orden de cambio que ampara los excesos", key=f"cp_oc_{bor['id']}") if CP.hay_exceso(con, bor["id"]) else ""
    x, y = st.columns(2)
    if x.button("Aprobar y pedir factura", type="primary", key=f"cp_ap_{bor['id']}", disabled=rol() not in ("admin", "gestor", "direccion")):
        try:
            mid = CP.aprobar(con, bor["id"], usuario(), rol(), oc or "")
            st.success("Aprobada. " + ("Correo al subcontratista preparado en «Correo saliente»." if mid else
                                        "Sin correo del subcontratista: descargue el PDF y envíelo."))
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    motivo = y.text_input("Motivo para anular", key=f"cp_mot_{bor['id']}")
    if y.button("Anular", key=f"cp_an_{bor['id']}"):
        try:
            CP.anular(con, bor["id"], usuario(), rol(), motivo); st.rerun()
        except ValueError as e:
            st.error(str(e))


def _listado(con, obra_id):
    CP.casar_facturas(con, obra_id)
    ls = CP.listado(con, obra_id)
    if not ls:
        st.caption("Aún no hay certificaciones.")
        return
    df = pd.DataFrame(ls)
    df["estado"] = df["estado"].map(CP.ESTADOS)
    for c in ("base_mes_cents", "base_origen_cents", "liquido_cents"):
        df[c] = df[c] / 100
    st.dataframe(df[["periodo", "proveedor", "numero", "estado", "base_mes_cents", "base_origen_cents", "liquido_cents", "aprobado_por", "factura_numero",
                     "exceso_referencia"]], hide_index=True, width="stretch",
                 column_config={"periodo": "Periodo", "proveedor": "Subcontrata", "numero": "Nº", "estado": "Estado",
                                "base_mes_cents": eur_col("Base mes (€)"), "base_origen_cents": eur_col("A origen (€)"),
                                "liquido_cents": eur_col("Líquido (€)"), "aprobado_por": "Aprobada por", "factura_numero": "Su factura",
                                "exceso_referencia": "Orden de cambio"})
    con_pdf = [x for x in ls if x["pdf_path"] and Path(x["pdf_path"]).exists()]
    if con_pdf:
        s = st.selectbox("Descargar autorización de facturación", [x["id"] for x in con_pdf], key="cp_pdf",
                         format_func=lambda i: next(f"{x['proveedor']} · nº {x['numero']} · {x['periodo']}" for x in con_pdf if x["id"] == i))
        x = next(x for x in con_pdf if x["id"] == s)
        st.download_button("Descargar PDF", Path(x["pdf_path"]).read_bytes(), file_name=Path(x["pdf_path"]).name, mime="application/pdf")
    sin = CP.aprobadas_sin_factura(con, obra_id)
    if sin:
        st.warning(f"{len(sin)} certificación(es) aprobadas aún sin factura del subcontratista: "
                   f"{_e(sum(x['base_mes_cents'] for x in sin))} de coste devengado (provisión de cierre).")
