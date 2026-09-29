"""Mis pendientes: la bandeja de trabajo de cada persona según su cargo."""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st

from core import db, ingesta as ing
from vistas.comun import get_con, usuario, rol, filtro_obras_sql, ir_a_documento, eur_col


def _dias(ts) -> int | None:
    try:
        return (date.today() - datetime.fromisoformat(str(ts).replace(" ", "T")[:19]).date()).days
    except Exception:
        return None


def _tabla(filas: list[dict], clave: str, vacio: str):
    if not filas:
        st.caption(vacio)
        return
    reglas = ing.reglas_aprobacion(get_con())
    df = pd.DataFrame(filas)
    df["dias"] = df["creado_en"].map(_dias)
    df["base"] = df["base_c"] / 100
    df["aviso"] = df["dias"].map(lambda d: "Retrasada" if d is not None and d > reglas["dias_aviso"] else "")
    st.dataframe(df[["id", "obra", "proveedor", "numero", "fecha", "base", "dias", "aviso", "incid"]], hide_index=True, width="stretch",
                 column_config={"base": eur_col("Base (€)"), "dias": st.column_config.NumberColumn("Días en espera"),
                                "incid": st.column_config.NumberColumn("Incidencias graves"), "aviso": "Aviso"})
    c1, c2 = st.columns([3, 1])
    sel = c1.selectbox("Abrir", df["id"].tolist(), key=f"pend_{clave}", label_visibility="collapsed",
                       format_func=lambda i: f"#{i} · {df.set_index('id').loc[i, 'proveedor'] or ''} · {df.set_index('id').loc[i, 'numero'] or ''}")
    if c2.button("Abrir en revisión", key=f"abrir_{clave}", icon=":material/open_in_new:"):
        ir_a_documento(sel)


def render():
    con = get_con()
    st.title("Mis pendientes")
    r = rol()
    reglas = ing.reglas_aprobacion(con)
    f_sql, f_par = filtro_obras_sql()
    base_q = f"""SELECT d.id, o.codigo AS obra, d.emisor_nombre AS proveedor, d.numero, d.fecha, d.base_imponible_cents AS base_c,
                        d.creado_en, d.estado, d.conformado_por,
                        (SELECT COUNT(*) FROM incidencias i WHERE i.documento_id=d.id AND i.resuelta=0
                           AND i.severidad IN ('critica','alta')) AS incid
                 FROM documentos d LEFT JOIN obras o ON o.id=d.obra_id
                 WHERE d.tipo_documento IN ('factura','abono','anticipo') {f_sql}"""
    tabs = []
    if r in ("jefe_obra", "admin", "direccion"):
        tabs.append(("Conformidad de mis obras", "conf",
                     base_q + " AND d.estado IN ('pendiente_revision','revisada') AND d.conformado_por IS NULL AND d.obra_id IN "
                              "(SELECT obra_id FROM obra_usuarios ou JOIN usuarios u ON u.id=ou.usuario_id WHERE u.rol='jefe_obra')"
                     + (" AND d.obra_id IN (SELECT obra_id FROM obra_usuarios WHERE usuario_id=?)" if r == "jefe_obra" else ""),
                     "No hay facturas esperando su conformidad."))
    if r in ("gestor", "admin", "direccion"):
        tabs.append(("Pendientes de revisar datos", "rev", base_q + " AND d.estado='pendiente_revision'", "Nada pendiente de revisar."))
        tabs.append(("Listas para aprobar", "apr",
                     base_q + " AND d.estado IN ('pendiente_revision','revisada') AND ABS(COALESCE(d.base_imponible_cents,0)) < ?",
                     "Nada listo para aprobar."))
    if r in ("admin", "direccion"):
        tabs.append((f"Aprobación de Dirección (≥ {reglas['umbral_cents'] / 100:,.0f} €)".replace(",", "."), "dir",
                     base_q + " AND d.estado IN ('pendiente_revision','revisada') AND ABS(COALESCE(d.base_imponible_cents,0)) >= ?",
                     "Nada pendiente de Dirección."))
    if not tabs:
        st.info("Su rol no tiene bandeja de aprobación.")
        return
    t_ui = st.tabs([t[0] for t in tabs])
    for (nombre, clave, q, vacio), t in zip(tabs, t_ui):
        with t:
            par = list(f_par)
            if clave == "conf" and r == "jefe_obra":
                par.append((st.session_state.get("auth") or {}).get("id"))
            if clave in ("apr", "dir"):
                par.append(reglas["umbral_cents"])
            filas = db.rows(con, q + " ORDER BY d.creado_en", par)
            if clave in ("apr", "dir"):
                filas = [f for f in filas if not [x for x in ing.requisitos_aprobacion(con, f["id"], None)]]
            _tabla(filas, clave, vacio)
    st.caption(f"Reglas: conformidad del jefe de obra {'obligatoria' if reglas['conformidad'] else 'opcional'} en obras con jefe asignado · "
               f"Dirección aprueba desde {reglas['umbral_cents'] / 100:,.0f} € · aviso a los {reglas['dias_aviso']} días. "
               "Se cambian en Configuración.".replace(",", "."))
