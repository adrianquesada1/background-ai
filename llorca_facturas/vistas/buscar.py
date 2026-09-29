"""Buscador global: facturas, líneas, proveedores, partidas certificadas y ofertas desde un único cuadro."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import db
from vistas.comun import get_con, eur_col, ir_a_documento, puede_editar


def render():
    con = get_con()
    st.title("Buscar")
    q = st.text_input("Buscar en toda la aplicación", key="buscar_q",
                      placeholder="Proveedor, CIF, nº de factura, material, partida, importe (p. ej. 8509,52)…")
    if len(q.strip()) < 2:
        st.caption("Escriba al menos dos caracteres. Se busca en facturas, líneas de factura, proveedores, partidas de la "
                   "certificación y ofertas.")
        return
    like = f"%{q.strip()}%"
    imp = q.strip().replace(".", "").replace(",", ".")
    try:
        cents = int(round(float(imp) * 100))
    except ValueError:
        cents = None
    from vistas.comun import filtro_obras_sql
    F_SQL, F_PAR = filtro_obras_sql()
    docs = pd.DataFrame(db.rows(con, f"""
        SELECT d.id, d.estado, d.emisor_nombre AS proveedor, d.emisor_nif AS nif, d.numero, d.fecha,
               d.base_imponible_cents/100.0 AS base, d.total_a_pagar_cents/100.0 AS a_pagar, d.concepto_general AS concepto, d.filename
        FROM documentos d
        WHERE (d.emisor_nombre LIKE ? OR d.emisor_nif LIKE ? OR d.numero LIKE ? OR d.concepto_general LIKE ? OR d.filename LIKE ?
              OR d.base_imponible_cents=? OR d.total_a_pagar_cents=? OR d.total_factura_cents=?) AND d.estado<>'eliminado' {F_SQL}
        ORDER BY d.fecha DESC LIMIT 200""", (like, like, like, like, like, cents, cents, cents, *F_PAR)))
    lin = pd.DataFrame(db.rows(con, f"""
        SELECT l.documento_id AS doc, d.emisor_nombre AS proveedor, d.numero, d.fecha, l.descripcion, l.cantidad, l.unidad,
               l.precio_unitario AS precio, l.importe_cents/100.0 AS importe, l.albaran
        FROM lineas l JOIN documentos d ON d.id=l.documento_id
        WHERE (l.descripcion LIKE ? OR l.albaran LIKE ? OR l.importe_cents=?) AND d.estado<>'eliminado' {F_SQL}
        ORDER BY d.fecha DESC LIMIT 300""", (like, like, cents, *F_PAR)))
    prov = pd.DataFrame(db.rows(con, """
        SELECT p.nombre, p.nif, COUNT(d.id) AS facturas, SUM(d.base_imponible_cents)/100.0 AS base
        FROM proveedores p LEFT JOIN documentos d ON d.proveedor_id=p.id
        WHERE p.nombre LIKE ? OR p.nif LIKE ? GROUP BY p.id ORDER BY base DESC LIMIT 100""", (like, like)))
    part = pd.DataFrame(db.rows(con, """
        SELECT c.numero AS cert, l.capitulo, l.codigo, l.titulo, l.unidad, l.precio, l.origen_m/1000.0 AS a_origen, l.actual_m/1000.0 AS mes
        FROM cert_lineas l JOIN certificaciones c ON c.id=l.cert_id
        WHERE (l.titulo LIKE ? OR l.codigo LIKE ? OR l.descripcion LIKE ?)
          AND c.id IN (SELECT MAX(id) FROM certificaciones GROUP BY obra_id) LIMIT 200""", (like, like, like)))
    ofe = pd.DataFrame(db.rows(con, """
        SELECT o.id, p.codigo AS capitulo, COALESCE(pr.nombre, o.proveedor_nombre) AS proveedor, o.estado,
               o.importe_cents/100.0 AS importe, o.valorado_por, o.opinion
        FROM ofertas o LEFT JOIN partidas p ON p.id=o.partida_id LEFT JOIN proveedores pr ON pr.id=o.proveedor_id
        WHERE COALESCE(pr.nombre, o.proveedor_nombre) LIKE ? OR o.alcance LIKE ? OR o.opinion LIKE ? LIMIT 100""", (like, like, like)))
    from core import indice
    from vistas.comun import obras_permitidas
    perm = obras_permitidas()
    ids_ok = None
    if perm is not None:
        ids_ok = {r["id"] for r in db.rows(con, f"SELECT id FROM documentos WHERE obra_id IN ({','.join('?' * len(perm))})", list(perm))} if perm else set()
    txt = indice.buscar(con, q, 200, ids_ok)
    tabs = st.tabs([f"Dentro de los documentos ({len(txt)})", f"Facturas ({len(docs)})", f"Líneas ({len(lin)})", f"Proveedores ({len(prov)})",
                    f"Partidas certificadas ({len(part)})", f"Ofertas ({len(ofe)})"])
    with tabs[0]:
        st.caption("Busca en el texto completo de todos los PDF (también los escaneados, tras su lectura), página a página. "
                   "Sin distinguir mayúsculas ni tildes; use comillas para una frase exacta: \"orden de cambio\".")
        pend_ix = indice.pendientes_de_indexar(con)
        if pend_ix:
            if st.button(f"Indexar {len(pend_ix)} documento(s) aún no incluidos en la búsqueda"):
                barra = st.progress(0.0)
                for k, did in enumerate(pend_ix, 1):
                    indice.indexar_documento(con, did)
                    barra.progress(k / len(pend_ix))
                st.rerun()
        if not txt:
            st.caption("Sin coincidencias en el texto de los documentos.")
        else:
            df_t = pd.DataFrame(txt)
            st.dataframe(df_t[["documento_id", "pagina", "fragmento", "emisor_nombre", "numero", "fecha", "obra", "filename"]],
                         hide_index=True, width="stretch", column_config={
                             "documento_id": "Doc.", "pagina": "Pág.", "fragmento": st.column_config.TextColumn("Texto encontrado", width="large"),
                             "emisor_nombre": "Proveedor", "numero": "Nº", "filename": "Archivo"})
            if puede_editar() or True:
                c1, c2 = st.columns([3, 1])
                opciones = [(r["documento_id"], r["pagina"]) for r in txt]
                sel = c1.selectbox("Abrir", opciones, label_visibility="collapsed", key="buscar_txt_sel",
                                   format_func=lambda x: f"#{x[0]} · página {x[1]} · " +
                                   next((r["filename"] for r in txt if r["documento_id"] == x[0]), ""))
                if c2.button("Abrir en esa página", icon=":material/open_in_new:", key="buscar_txt_ir"):
                    ir_a_documento(sel[0], sel[1])
    with tabs[1]:
        if docs.empty:
            st.caption("Sin resultados.")
        else:
            st.dataframe(docs, hide_index=True, width="stretch",
                         column_config={"base": eur_col("Base (€)"), "a_pagar": eur_col("A pagar (€)")})
            if puede_editar():
                c1, c2 = st.columns([3, 1])
                sel = c1.selectbox("Documento", docs["id"].tolist(), label_visibility="collapsed",
                                   format_func=lambda i: f"#{i} · {docs.set_index('id').loc[i, 'proveedor'] or ''} · "
                                                         f"{docs.set_index('id').loc[i, 'numero'] or ''}")
                if c2.button("Abrir en revisión", icon=":material/open_in_new:"):
                    ir_a_documento(sel)
    with tabs[2]:
        if lin.empty:
            st.caption("Sin resultados.")
        else:
            st.dataframe(lin, hide_index=True, width="stretch", column_config={"importe": eur_col("Importe (€)")})
            st.caption(f"Suma de las líneas encontradas: {lin['importe'].sum():,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))
    with tabs[3]:
        st.dataframe(prov, hide_index=True, width="stretch", column_config={"base": eur_col("Base facturada (€)")}) \
            if not prov.empty else st.caption("Sin resultados.")
    with tabs[4]:
        st.dataframe(part, hide_index=True, width="stretch", column_config={"a_origen": eur_col("A origen (€)"),
                                                                           "mes": eur_col("Este mes (€)")}) \
            if not part.empty else st.caption("Sin resultados.")
    with tabs[5]:
        st.dataframe(ofe, hide_index=True, width="stretch", column_config={"importe": eur_col("Importe (€)")}) \
            if not ofe.empty else st.caption("Sin resultados.")
