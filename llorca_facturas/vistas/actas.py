"""Actas desde el audio de la reunión: transcripción local, propuesta de compromisos y paso al diario de obra."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import db, actas_audio as AA, gobierno as G, planificador
from vistas.comun import get_con, usuario, rol, selector_obra


def render():
    con = get_con()
    st.title("Actas desde audio")
    st.caption("Suba la grabación de la reunión. Se transcribe en este equipo (el audio no sale de la oficina); de la transcripción salen "
               "propuestas de compromisos, decisiones y posibles extras. Revíselas y páselas al diario de obra.")
    motor, msg = AA.motor_disponible()
    if not motor:
        st.warning(msg)
    obra_id = selector_obra("act_obra", permitir_todas=False)
    if not obra_id:
        return
    edita = rol() in ("admin", "gestor", "direccion", "jefe_obra", "tecnico")
    if edita:
        with st.form("act_subir", clear_on_submit=True):
            c = st.columns([2, 1])
            tit = c[0].text_input("Título", "Reunión de obra")
            fecha = c[1].date_input("Fecha", date.today(), format="DD/MM/YYYY")
            asist = st.text_input("Asistentes")
            up = st.file_uploader("Grabación", type=AA.EXT_AUDIO)
            if st.form_submit_button("Subir y transcribir", type="primary"):
                if not up:
                    st.error("Seleccione el audio.")
                else:
                    try:
                        AA.subir(con, obra_id, tit, fecha.isoformat(), asist, up.name, up.getvalue(), usuario())
                        st.success("En cola: se transcribe en segundo plano (en CPU tarda del orden de la duración del audio).")
                    except ValueError as e:
                        st.error(str(e))
    actas = db.rows(con, "SELECT * FROM actas_audio WHERE obra_id=? ORDER BY id DESC LIMIT 50", (obra_id,))
    if not actas:
        st.caption("Aún no hay grabaciones.")
        return
    df = pd.DataFrame(actas)
    df["estado"] = df["estado"].map(AA.ESTADOS)
    st.dataframe(df[["fecha", "titulo", "estado", "duracion_s", "segundos_proceso", "modelo", "error"]], hide_index=True, width="stretch",
                 column_config={"fecha": "Fecha", "titulo": "Reunión", "estado": "Estado", "duracion_s": "Audio (s)", "segundos_proceso": "Proceso (s)",
                                "modelo": "Motor", "error": "Error"})
    if any(a["estado"] == "pendiente" for a in actas) and st.button("Transcribir ahora"):
        with st.spinner("Transcribiendo…"):
            r = planificador.ejecutar_ahora(con, "actas")
        (st.success if r["ok"] else st.error)(r["resultado"] or r["error"])
    listas = [a for a in actas if a["estado"] in ("transcrita", "error")]
    if not listas:
        return
    aid = st.selectbox("Revisar", [a["id"] for a in listas], key="act_sel",
                       format_func=lambda i: next(f"{a['fecha']} · {a['titulo']} · {AA.ESTADOS[a['estado']]}" for a in listas if a["id"] == i))
    a = next(x for x in listas if x["id"] == aid)
    if a["estado"] == "error":
        st.error(a["error"])
        if edita and st.button("Reintentar"):
            AA.reintentar(con, aid, usuario()); st.rerun()
        return
    if a["resumen"]:
        st.markdown("**Resumen (IA local, revíselo)**")
        st.markdown(a["resumen"])
    notas = st.text_area("Acta / notas de la reunión", (a["resumen"] + "\n\n---\nTranscripción:\n" if a["resumen"] else "") + AA.texto_con_tiempos(a),
                         height=320, key=f"act_notas_{aid}")
    prop = pd.DataFrame(AA.propuestas(a) or [], columns=["tipo", "descripcion", "responsable", "fecha_limite", "importe"])
    prop.insert(0, "incluir", True)
    st.markdown("**Compromisos, decisiones y extras detectados**")
    ed = st.data_editor(prop, num_rows="dynamic", hide_index=True, width="stretch", key=f"act_prop_{aid}",
                        column_config={"incluir": "Incluir", "tipo": st.column_config.SelectboxColumn("Tipo", options=list(G.TIPOS)),
                                       "descripcion": st.column_config.TextColumn("Descripción", width="large"), "responsable": "Responsable",
                                       "fecha_limite": "Fecha límite (AAAA-MM-DD)", "importe": "Importe estimado"})
    if edita and st.button("Crear la reunión en el diario de obra", type="primary"):
        try:
            sel = [r for r in ed.fillna("").to_dict("records") if r.get("incluir")]
            AA.crear_reunion(con, aid, obra_id, a["fecha"] or date.today().isoformat(), a["titulo"], a["asistentes"] or "", notas, sel, usuario())
            st.success("Reunión creada en el diario de obra, con sus compromisos."); st.rerun()
        except ValueError as e:
            st.error(str(e))
