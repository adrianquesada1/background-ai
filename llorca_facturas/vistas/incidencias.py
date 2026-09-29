"""Bandeja global de incidencias: lo que hay que mirar antes de contabilizar o pagar."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import db
from core.validation import validar_y_guardar
from vistas.comun import get_con, icono_sev, ir_a_documento, selector_obra
from core.config import SEVERIDAD_TEXTO

CODIGOS = {
    "C01": "Líneas ≠ base", "C02": "Cuota IVA", "C03": "Bases IVA", "C04": "Total factura", "C05": "Ret. garantía",
    "C06": "IRPF", "C07": "Líquido a pagar", "C08": "IVA / ISP", "C09": "NIF emisor", "C10": "Receptor",
    "C11": "IBAN", "C12": "Duplicado", "C13": "Fechas", "C14": "Obra", "C15": "Cálculo de línea",
    "C16": "Anclaje al PDF", "C17": "Signo", "C18": "Calidad lectura", "C19": "Partidas", "C20": "Nº factura",
    "C21": "Doc. soporte", "C22": "Sin retención",
}


def render():
    con = get_con()
    st.title("Incidencias")
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        obra_id = selector_obra("inc_obra")
    sev = c2.multiselect("Severidad", ["critica", "alta", "media", "info"], default=["critica", "alta", "media"],
                         format_func=lambda s: SEVERIDAD_TEXTO[s])
    ver_res = c3.checkbox("Incluir resueltas")
    sql = """SELECT i.id, i.severidad, i.codigo, i.mensaje, i.detalle, i.resuelta, i.resuelta_por, i.comentario,
                    d.id AS doc, d.emisor_nombre AS proveedor, d.numero, d.estado
             FROM incidencias i JOIN documentos d ON d.id=i.documento_id
             WHERE d.estado<>'rechazada'"""
    p = []
    if sev:
        sql += f" AND i.severidad IN ({','.join('?'*len(sev))})"; p += sev
    if not ver_res:
        sql += " AND i.resuelta=0"
    if obra_id:
        sql += " AND d.obra_id=?"; p.append(obra_id)
    from vistas.comun import filtro_obras_sql
    f_sql, f_par = filtro_obras_sql()
    sql += f_sql
    p += f_par
    sql += " ORDER BY CASE i.severidad WHEN 'critica' THEN 0 WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END, d.id"
    df = pd.DataFrame(db.rows(con, sql, p))
    if df.empty:
        st.success("Nada pendiente con estos filtros.")
    else:
        resumen = df.groupby(["severidad", "codigo"]).size().reset_index(name="n")
        resumen["control"] = resumen["codigo"].map(CODIGOS)
        m = st.columns(4)
        for k, s in enumerate(["critica", "alta", "media", "info"]):
            m[k].metric(SEVERIDAD_TEXTO[s], int((df["severidad"] == s).sum()))
        df["sev"] = df["severidad"].map(SEVERIDAD_TEXTO)
        df["control"] = df["codigo"].map(CODIGOS)
        st.dataframe(df[["sev", "control", "mensaje", "proveedor", "numero", "doc", "detalle", "resuelta", "resuelta_por", "comentario"]],
                     hide_index=True, width="stretch",
                     column_config={"sev": "Severidad", "resuelta": st.column_config.CheckboxColumn("Resuelta"),
                                    "detalle": st.column_config.TextColumn("Detalle", width="large")})
        c1, c2 = st.columns([3, 1])
        docs = df.drop_duplicates("doc")
        sel = c1.selectbox("Documento", docs["doc"].tolist(), label_visibility="collapsed",
                           format_func=lambda d: f"#{d} · {docs.set_index('doc').loc[d, 'proveedor'] or ''} · "
                                                 f"{docs.set_index('doc').loc[d, 'numero'] or ''}")
        if c2.button("Abrir en revisión", icon=":material/open_in_new:"):
            ir_a_documento(sel)
    st.divider()
    if st.button("Revalidar todos los documentos",
                 help="Vuelve a pasar todos los controles (útil tras cambiar maestros, IBAN verificados o tolerancias)."):
        ids = [r["id"] for r in db.rows(con, "SELECT id FROM documentos WHERE estado<>'sin_procesar'")]
        barra = st.progress(0.0)
        for k, d in enumerate(ids, 1):
            validar_y_guardar(con, d)
            barra.progress(k / len(ids))
        st.success(f"{len(ids)} documentos revalidados."); st.rerun()
