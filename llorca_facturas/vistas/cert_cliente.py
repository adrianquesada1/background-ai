"""Preparar la certificación al cliente a partir de lo certificado a las subcontratas."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from core import cert_cliente as CC, obra_control
from core.money import fmt_eur
from vistas.comun import get_con, usuario, rol, selector_obra, eur_col


def render():
    con = get_con()
    st.title("Preparar la certificación al cliente")
    st.caption("Propuesta de la certificación del mes con la estructura y los precios de la última certificación al cliente y el avance "
               "medido a las subcontratas (líneas de contrato asociadas a cada partida). Nunca baja de lo ya certificado; lo que no tiene "
               "subcontrata se mantiene para que el jefe de obra lo mida. Se ajusta a mano y se descarga en Excel.")
    obra_id = selector_obra("ccl_obra", permitir_todas=False)
    if not obra_id:
        return
    certs = obra_control.certificaciones(con, obra_id)
    if not certs:
        st.info("La obra aún no tiene ninguna certificación al cliente importada: importe la última en «Certificaciones» para tomar su estructura.")
        return
    c = st.columns([1, 2])
    periodo = c[0].text_input("Periodo a certificar (AAAA-MM)", date.today().strftime("%Y-%m"), key="ccl_per")
    base_id = c[1].selectbox("Certificación base (estructura, precios y lo ya certificado)", [x["id"] for x in reversed(certs)], key="ccl_base",
                             format_func=lambda i: next(f"nº {x['numero']} · {x['fecha']}" for x in certs if x["id"] == i))
    bor = CC.cargar_borrador(con, obra_id, periodo)
    ajustes = (bor or {}).get("ajustes", {})
    try:
        p = CC.proponer(con, obra_id, periodo, base_id, ajustes)
    except ValueError as e:
        st.error(str(e))
        return
    t = p["totales"]
    k = st.columns(5)
    k[0].metric("Venta propuesta del mes", fmt_eur(t["mes"]))
    k[1].metric("A origen", fmt_eur(t["origen"]))
    k[2].metric("Coste subcontratas del mes", fmt_eur(t["coste_proveedores_mes"]), help="Certificaciones a subcontratas aprobadas en el periodo.")
    k[3].metric("Margen del mes", fmt_eur(t["margen_mes"]),
                delta=f"{(t['margen_mes'] / t['mes'] * 100):.1f} %" if t["mes"] else None)
    k[4].metric("Partidas sin medición", t["partidas_sin_medicion"], help="Sin subcontrata asociada: se mantiene lo certificado.")
    for a in p["avisos"]:
        st.warning(a)
    solo = st.toggle("Ver solo partidas con avance o con aviso", True, key="ccl_solo")
    df = pd.DataFrame([{**l, "ajuste": ajustes.get(str(l["id"]), "")} for l in p["lineas"]])
    if solo:
        df = df[(df["cant_mes"] != 0) | (df["ajuste"] != "") | (df["aviso"].ne("") & ~df["aviso"].str.startswith("Sin subcontrata"))]
    if df.empty:
        st.info("No hay avance medido a subcontratas para este periodo.")
    else:
        vis = pd.DataFrame({"id": df["id"], "capitulo": df["capitulo"], "codigo": df["codigo"], "titulo": df["titulo"], "unidad": df["unidad"],
                            "precio": df["precio"].map(float), "anterior": df["cant_anterior"].map(float), "propuesta": df["cant_origen"].map(float),
                            "ajuste": df["ajuste"].astype(str), "mes": df["importe_mes"].map(float), "fuente": df["fuente"], "aviso": df["aviso"]})
        ed = st.data_editor(vis, hide_index=True, width="stretch", key=f"ccl_ed_{periodo}_{base_id}",
                            disabled=[c for c in vis.columns if c != "ajuste"],
                            column_config={"id": None, "capitulo": "Cap.", "codigo": "Código", "titulo": st.column_config.TextColumn("Partida", width="medium"),
                                           "unidad": "Ud", "precio": "Precio", "anterior": "Cert. anterior", "propuesta": "Propuesta a origen",
                                           "ajuste": st.column_config.TextColumn("Ajuste manual a origen", help="Déjelo vacío para aceptar la propuesta."),
                                           "mes": eur_col("Venta mes (€)"), "fuente": "Medición de subcontratas", "aviso": "Aviso"})
        if rol() in ("admin", "gestor", "direccion", "jefe_obra", "tecnico") and st.button("Guardar ajustes", type="primary"):
            nuevos = dict(ajustes)
            for r in ed.to_dict("records"):
                v = str(r["ajuste"] or "").strip().replace(",", ".")
                if v:
                    try:
                        Decimal(v)
                    except Exception:
                        st.error(f"Ajuste no válido en {r['codigo']}: {r['ajuste']}")
                        return
                    nuevos[str(r["id"])] = v
                else:
                    nuevos.pop(str(r["id"]), None)
            CC.guardar_borrador(con, obra_id, periodo, base_id, nuevos, usuario())
            st.success("Borrador guardado."); st.rerun()
    with st.expander("Resumen por capítulos"):
        cap = pd.DataFrame(p["capitulos"])
        if not cap.empty:
            for c_ in ("importe_anterior", "importe_origen", "importe_mes"):
                cap[c_] = cap[c_].map(float)
            st.dataframe(cap, hide_index=True, width="stretch",
                         column_config={"capitulo": "Cap.", "nombre": "Capítulo", "partidas": "Partidas", "importe_anterior": eur_col("Anterior (€)"),
                                        "importe_origen": eur_col("Origen (€)"), "importe_mes": eur_col("Mes (€)")})
    st.download_button("Descargar propuesta en Excel", CC.excel(p), file_name=f"propuesta_certificacion_{periodo}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    posteriores = [x for x in certs if x["id"] != base_id and x["numero"] > next(c["numero"] for c in certs if c["id"] == base_id)]
    if posteriores:
        with st.expander("Comparar con la certificación definitiva importada"):
            real = st.selectbox("Certificación definitiva", [x["id"] for x in posteriores],
                                format_func=lambda i: next(f"nº {x['numero']} · {x['fecha']}" for x in posteriores if x["id"] == i))
            dif = CC.comparar_con_real(con, p, real)
            if dif:
                d = pd.DataFrame(dif)
                for c_ in ("propuesta", "real", "diferencia", "diferencia_eur"):
                    d[c_] = d[c_].map(float)
                st.dataframe(d, hide_index=True, width="stretch")
            else:
                st.success("La certificación definitiva coincide con la propuesta.")
