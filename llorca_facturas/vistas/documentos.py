"""Explorador de documentos y marcado de pagos / devolución de retenciones."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import db
from core.config import ESTADO_LABEL, TIPOS_DOCUMENTO
from core.money import from_cents, fmt_eur
from vistas.comun import get_con, usuario, selector_obra, eur_col, ir_a_documento, puede_editar


def _asientos(con):
    from core import asientos as AS
    from vistas.comun import selector_obra as _sel
    import io as _io
    st.caption("Asientos propuestos de las facturas APROBADAS (gasto, IVA soportado, ISP, IRPF, retención de garantía y proveedor), con "
               "la ruta del PDF para adjuntarlo. Cada asiento cuadra. Cuentas configurables en Configuración.")
    c1, c2 = st.columns(2)
    desde = c1.date_input("Desde", value=None, format="DD/MM/YYYY", key="as_desde")
    hasta = c2.date_input("Hasta", value=None, format="DD/MM/YYYY", key="as_hasta")
    filas = AS.proponer(con, desde.isoformat() if desde else None, hasta.isoformat() if hasta else None)
    if not filas:
        st.caption("No hay facturas aprobadas en ese periodo.")
        return
    malos = AS.cuadran(filas)
    if malos:
        st.error(f"Asientos que no cuadran: {malos}")
    df = pd.DataFrame(filas)
    df["debe"] = df["debe"].map(float); df["haber"] = df["haber"].map(float)
    st.dataframe(df.drop(columns=["pdf", "doc_id"]), hide_index=True, width="stretch", height=320)
    buf = _io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Asientos", index=False)
    st.download_button("Descargar asientos (Excel)", buf.getvalue(), file_name="asientos_facturas_recibidas.xlsx")
    st.download_button("Descargar asientos (CSV ; )", df.to_csv(sep=";", index=False, decimal=",").encode("utf-8-sig"),
                       file_name="asientos_facturas_recibidas.csv")


def render():
    con = get_con()
    st.title("Documentos y pagos")
    with st.expander("Asientos contables para SIS"):
        _asientos(con)
    with st.expander("Papelera (documentos eliminados, restaurables)"):
        papelera(con)
    c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
    with c1:
        obra_id = selector_obra("doc_obra")
    estados = c2.multiselect("Estado", list(ESTADO_LABEL), format_func=lambda e: ESTADO_LABEL[e], key="doc_est")
    tipos = c3.multiselect("Tipo", TIPOS_DOCUMENTO, key="doc_tipo")
    texto = c4.text_input("Buscar", placeholder="proveedor, nº, concepto…", key="doc_txt")

    sql = """SELECT d.id, d.estado, d.tipo_documento AS tipo, o.codigo AS obra, d.emisor_nombre AS proveedor,
                    d.emisor_nif AS nif, d.numero, d.fecha, d.fecha_vencimiento AS vencimiento,
                    d.base_imponible_cents AS base_c, d.total_iva_cents AS iva_c, d.ret_garantia_cents AS ret_c,
                    d.total_a_pagar_cents AS pagar_c, d.pagada, d.fecha_pago, d.ret_garantia_devuelta,
                    d.concepto_general AS concepto, d.filename
             FROM documentos d LEFT JOIN obras o ON o.id=d.obra_id WHERE 1=1"""
    p = []
    if obra_id:
        sql += " AND d.obra_id=?"; p.append(obra_id)
    if estados:
        sql += f" AND d.estado IN ({','.join('?'*len(estados))})"; p += estados
    if tipos:
        sql += f" AND d.tipo_documento IN ({','.join('?'*len(tipos))})"; p += tipos
    if texto:
        sql += " AND (d.emisor_nombre LIKE ? OR d.numero LIKE ? OR d.concepto_general LIKE ? OR d.filename LIKE ? OR d.emisor_nif LIKE ?)"
        p += [f"%{texto}%"] * 5
    if not estados:
        sql += " AND d.estado<>'eliminado'"
    from vistas.comun import filtro_obras_sql
    f_sql, f_par = filtro_obras_sql()
    sql += f_sql
    p += f_par
    sql += " ORDER BY d.fecha DESC, d.id DESC"
    df = pd.DataFrame(db.rows(con, sql, p))
    if df.empty:
        st.info("Sin documentos.")
        return

    m = st.columns(4)
    m[0].metric("Documentos", len(df))
    m[1].metric("Σ bases", fmt_eur(from_cents(df["base_c"].fillna(0).sum())))
    m[2].metric("Σ a pagar", fmt_eur(from_cents(df["pagar_c"].fillna(0).sum())))
    m[3].metric("Pendiente de pago", fmt_eur(from_cents(df.loc[df["pagada"] == 0, "pagar_c"].fillna(0).sum())))

    vis = df.copy()
    for c, n in {"base_c": "base", "iva_c": "iva", "ret_c": "ret", "pagar_c": "a_pagar"}.items():
        vis[n] = vis[c].fillna(0).astype("int64") / 100
    vis = vis.drop(columns=["base_c", "iva_c", "ret_c", "pagar_c"])
    vis["pagada"] = vis["pagada"].astype(bool)
    vis["ret_garantia_devuelta"] = vis["ret_garantia_devuelta"].astype(bool)
    vis["estado"] = vis["estado"].map(ESTADO_LABEL)
    bloqueadas = [c for c in vis.columns if c not in ("pagada", "fecha_pago", "ret_garantia_devuelta")]
    ed = st.data_editor(vis, hide_index=True, width="stretch", disabled=bloqueadas, key="doc_editor",
                        column_order=["id", "estado", "tipo", "obra", "proveedor", "numero", "fecha", "vencimiento", "base",
                                      "iva", "ret", "a_pagar", "pagada", "fecha_pago", "ret_garantia_devuelta", "concepto",
                                      "nif", "filename"],
                        column_config={"base": eur_col("Base (€)"), "iva": eur_col("IVA (€)"), "ret": eur_col("Ret. gar. (€)"),
                                       "a_pagar": eur_col("A pagar (€)"), "pagada": st.column_config.CheckboxColumn("Pagada"),
                                       "fecha_pago": st.column_config.TextColumn("Fecha pago", help="YYYY-MM-DD"),
                                       "ret_garantia_devuelta": st.column_config.CheckboxColumn("Ret. devuelta")})
    cambios = []
    orig = vis.set_index("id")
    for r in ed.itertuples():
        o = orig.loc[r.id]
        if bool(r.pagada) != bool(o["pagada"]) or (r.fecha_pago or None) != (o["fecha_pago"] or None) \
                or bool(r.ret_garantia_devuelta) != bool(o["ret_garantia_devuelta"]):
            cambios.append((int(r.id), int(bool(r.pagada)), r.fecha_pago or None, int(bool(r.ret_garantia_devuelta))))
    c1, c2 = st.columns([1, 3])
    if c1.button(f"Guardar {len(cambios)} cambio(s) de pago", type="primary", disabled=not cambios or not puede_editar()):
        no_aprob = [str(did) for did, pag, _, _ in cambios if pag and db.one(con, "SELECT estado FROM documentos WHERE id=?", (did,))["estado"] != "aprobada"]
        if no_aprob:
            st.warning(f"Aviso: se marcan como pagadas facturas que aún no están aprobadas (#{', #'.join(no_aprob)}).")
        with db.tx(con):
            for did, pag, fp, dev in cambios:
                con.execute("UPDATE documentos SET pagada=?, fecha_pago=?, ret_garantia_devuelta=? WHERE id=?", (pag, fp, dev, did))
                db.audit(con, usuario(), "pago", "documento", did, {"pagada": pag, "fecha_pago": fp, "ret_devuelta": dev})
        st.toast("Guardado"); st.rerun()
    with c2:
        d1, d2 = st.columns([2, 1])
        ir = d1.selectbox("Abrir en revisión", df["id"].tolist(), key="doc_ir", label_visibility="collapsed",
                          format_func=lambda i: f"#{i} · {orig.loc[i, 'proveedor'] or ''} · {orig.loc[i, 'numero'] or ''}")
        if puede_editar() and d2.button("Abrir en revisión", icon=":material/open_in_new:"):
            ir_a_documento(ir)


def papelera(con):
    from core import ingesta as ing
    from vistas.comun import rol
    st.subheader("Papelera")
    pap = db.rows(con, "SELECT id, filename, emisor_nombre, numero, estado_previo FROM documentos WHERE estado='eliminado' ORDER BY id DESC")
    if not pap:
        st.caption("La papelera está vacía.")
        return
    st.dataframe(pd.DataFrame(pap), hide_index=True, width="stretch")
    c1, c2, c3 = st.columns([3, 1, 1])
    sel = c1.selectbox("Documento", [x["id"] for x in pap], label_visibility="collapsed",
                       format_func=lambda i: next(f"#{x['id']} · {x['filename']}" for x in pap if x["id"] == i))
    if c2.button("Restaurar", icon=":material/restore_from_trash:", disabled=not puede_editar()):
        ing.restaurar(con, sel, usuario()); st.rerun()
    if c3.button("Borrar definitivamente", disabled=rol() != "admin", help="Solo administradores. No se puede deshacer."):
        ing.borrar_definitivo(con, sel, usuario()); st.rerun()
