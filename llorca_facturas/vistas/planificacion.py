"""Planificación de obra: planning inicial, camino crítico, línea base y replanificación por retrasos."""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from core import planificacion as PL
from vistas.comun import get_con, usuario, rol, selector_obra
from vistas.estilo import ROJO, GRAFITO

COLS = ["codigo", "nombre", "duracion", "predecesoras", "responsable", "capitulo", "inicio_min", "inicio_real", "fin_real", "avance", "restante", "notas"]


def render():
    con = get_con()
    st.title("Planificación de obra")
    st.caption("Actividades con duración en días laborables y predecesoras (p. ej. «A10», «A20CC+2», «A30FF-1»). El camino crítico se "
               "calcula solo. Congele el planning aprobado como línea base; después, con lo real y la fecha de control, la replanificación "
               "enseña qué se mueve por cada retraso y cuánto se va el fin de obra.")
    obra_id = selector_obra("pl_obra", permitir_todas=False)
    if not obra_id:
        return
    edita = rol() in ("admin", "gestor", "direccion", "jefe_obra", "tecnico")
    cfg = PL.config(con, obra_id)
    with st.expander("Calendario y fecha de control", expanded=not PL.actividades(con, obra_id)):
        with st.form(f"pl_cfg_{obra_id}"):
            c = st.columns(3)
            ini = c[0].date_input("Inicio de obra", PL._fecha(cfg["inicio_obra"]), format="DD/MM/YYYY")
            fc = c[1].date_input("Fecha de control (lo real hasta)", PL._fecha(cfg["fecha_control"]) if cfg.get("fecha_control") else None,
                                 format="DD/MM/YYYY")
            sab = c[2].checkbox("Se trabajan los sábados", bool(cfg.get("sabados")))
            fest = st.text_area("Festivos y cierres (fechas o rangos «inicio..fin», separados por comas)", cfg.get("festivos") or "",
                                placeholder="2026-08-03..2026-08-21, 2026-10-12, 2026-12-08, 2026-12-24..2027-01-06")
            if st.form_submit_button("Guardar calendario", disabled=not edita):
                PL.guardar_config(con, obra_id, ini.isoformat(), fest, sab, fc.isoformat() if fc else None, usuario()); st.rerun()
    t1, t2, t3 = st.tabs(["Actividades", "Diagrama de Gantt", "Replanificación"])
    with t1:
        acts = PL.actividades(con, obra_id)
        up = st.file_uploader("Importar desde Excel/CSV (id, actividad, duración, predecesoras, responsable…)", type=["xlsx", "xls", "csv"], key=f"pl_up_{obra_id}")
        df = pd.DataFrame(acts)[COLS] if acts else pd.DataFrame(columns=COLS)
        if up is not None:
            try:
                df = pd.DataFrame(PL.leer_excel(up.getvalue(), up.name)).reindex(columns=COLS)
                st.info(f"{len(df)} actividad(es) leídas: revíselas y guarde.")
            except ValueError as e:
                st.error(str(e))
        for c_ in ("inicio_min", "inicio_real", "fin_real"):
            df[c_] = pd.to_datetime(df[c_], errors="coerce").dt.date
        df["duracion"] = pd.to_numeric(df["duracion"], errors="coerce").fillna(0).astype(int)
        df["avance"] = pd.to_numeric(df["avance"], errors="coerce").fillna(0).astype(int)
        df["restante"] = pd.to_numeric(df["restante"], errors="coerce")
        ed = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key=f"pl_ed_{obra_id}", disabled=not edita,
                            column_config={"codigo": "Id", "nombre": st.column_config.TextColumn("Actividad", width="medium"),
                                           "duracion": st.column_config.NumberColumn("Días lab.", min_value=0, step=1),
                                           "predecesoras": "Predecesoras", "responsable": "Responsable", "capitulo": "Capítulo",
                                           "inicio_min": st.column_config.DateColumn("No antes de", format="DD/MM/YYYY"),
                                           "inicio_real": st.column_config.DateColumn("Inicio real", format="DD/MM/YYYY"),
                                           "fin_real": st.column_config.DateColumn("Fin real", format="DD/MM/YYYY"),
                                           "avance": st.column_config.NumberColumn("% avance", min_value=0, max_value=100),
                                           "restante": st.column_config.NumberColumn("Días que faltan", min_value=0), "notas": "Notas"})
        if edita and st.button("Guardar actividades", type="primary"):
            try:
                filas = ed.to_dict("records")
                for f in filas:
                    for k in ("inicio_min", "inicio_real", "fin_real"):
                        f[k] = f[k].isoformat() if hasattr(f.get(k), "isoformat") and not pd.isna(f.get(k)) else None
                    if pd.isna(f.get("restante")):
                        f["restante"] = None
                n = PL.guardar_actividades(con, obra_id, filas, usuario())
                st.success(f"{n} actividad(es) guardadas."); st.rerun()
            except ValueError as e:
                st.error(str(e))
    if not PL.actividades(con, obra_id):
        return
    try:
        p = PL.planificar(con, obra_id)
    except ValueError as e:
        st.error(str(e))
        return
    for e in p["errores"]:
        st.warning(e)
    with t2:
        k = st.columns(3)
        k[0].metric("Fin de obra previsto", p["fin_obra"].strftime("%d/%m/%Y"))
        k[1].metric("Duración (días laborables)", p["duracion_total"])
        k[2].metric("Actividades críticas", sum(1 for a in p["actividades"] if a["critica"]))
        g = pd.DataFrame([{"Actividad": f"{a['codigo']} · {a['nombre']}", "Inicio": a["inicio"], "Fin": pd.Timestamp(a["fin"]) + pd.Timedelta(days=1),
                           "Tipo": "Crítica" if a["critica"] else "Con holgura", "Holgura": a["holgura"], "Responsable": a["responsable"] or ""}
                          for a in p["actividades"]])
        fig = px.timeline(g, x_start="Inicio", x_end="Fin", y="Actividad", color="Tipo", hover_data=["Holgura", "Responsable"],
                          color_discrete_map={"Crítica": ROJO, "Con holgura": "#9A968C"})
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(height=max(320, 28 * len(g) + 120), margin=dict(l=10, r=10, t=30, b=10), legend_title_text="", font_color=GRAFITO)
        cfgc = PL.config(con, obra_id)
        if cfgc.get("fecha_control"):
            fig.add_vline(x=pd.Timestamp(cfgc["fecha_control"]).timestamp() * 1000, line_dash="dot", line_color=GRAFITO)
        st.plotly_chart(fig, width="stretch")
        tabla = pd.DataFrame(p["actividades"])[["codigo", "nombre", "responsable", "duracion", "inicio", "fin", "holgura", "critica", "avance"]]
        st.dataframe(tabla, hide_index=True, width="stretch",
                     column_config={"codigo": "Id", "nombre": "Actividad", "responsable": "Responsable", "duracion": "Días", "inicio": "Inicio",
                                    "fin": "Fin", "holgura": "Holgura (días)", "critica": "Crítica", "avance": "% avance"})
        ret = PL.retrasadas(con, obra_id)
        if ret:
            st.warning(f"{len(ret)} actividad(es) no constan como empezadas/terminadas y ya deberían: " +
                       "; ".join(f"{a['codigo']} {a['nombre']} ({a['problema']})" for a in ret[:8]) + ". Actualice lo real y la fecha de control.")
    with t3:
        lbs = PL.lineas_base(con, obra_id)
        c = st.columns([2, 1])
        nombre = c[0].text_input("Nombre de la línea base", f"Planning aprobado {date.today():%d/%m/%Y}")
        if edita and c[1].button("Congelar línea base", help="Guarda el planning actual (sin lo real) para compararlo después."):
            PL.congelar_linea_base(con, obra_id, nombre, usuario()); st.rerun()
        if not lbs:
            st.info("Aún no hay línea base: congele el planning inicial aprobado.")
            return
        lb = st.selectbox("Comparar con", [x["id"] for x in lbs], format_func=lambda i: next(f"{x['nombre']} ({x['creada_en'][:10]})" for x in lbs if x["id"] == i))
        r = PL.replanificar(con, obra_id, lb)
        k = st.columns(3)
        k[0].metric("Fin de obra en la línea base", r["base"]["fin_obra"].strftime("%d/%m/%Y"))
        k[1].metric("Fin de obra replanificado", r["actual"]["fin_obra"].strftime("%d/%m/%Y"),
                    delta=f"{r['retraso_fin_dias']:+d} días lab." if r["retraso_fin_dias"] else "en plazo", delta_color="inverse")
        k[2].metric("Actividades desplazadas", len(r["cambios"]))
        if r["cambios"]:
            cam = pd.DataFrame(r["cambios"])
            cols = [c_ for c_ in ["codigo", "nombre", "responsable", "inicio_base", "inicio", "desplaz_inicio", "fin_base", "fin", "desplaz_fin", "critica", "motivo"]
                    if c_ in cam.columns]
            st.dataframe(cam[cols].sort_values("desplaz_fin", ascending=False), hide_index=True, width="stretch",
                         column_config={"codigo": "Id", "nombre": "Actividad", "responsable": "Responsable (avisar)", "inicio_base": "Inicio base",
                                        "inicio": "Inicio nuevo", "desplaz_inicio": "Δ inicio", "fin_base": "Fin base", "fin": "Fin nuevo",
                                        "desplaz_fin": "Δ fin", "critica": "Crítica", "motivo": "Motivo"})
            st.caption("Δ en días laborables. Avise a los responsables de las actividades desplazadas (subcontratas y suministros).")
        else:
            st.success("Todo según la línea base.")
