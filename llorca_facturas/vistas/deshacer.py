"""Papelera y deshacer: un único sitio para recuperar cualquier cosa borrada o cambiada por error."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import db, historial as H, ingesta as ing, trabajos
from vistas.comun import get_con, usuario, rol, puede_editar


@st.dialog("Confirmar")
def _confirmar(texto: str, accion, *args):
    st.markdown(texto)
    c1, c2 = st.columns(2)
    if c1.button("Sí, continuar", type="primary", width="stretch"):
        try:
            r = accion(*args)
            st.session_state["_msg_deshacer"] = f"Hecho. {r if isinstance(r, str) else ''}"
        except Exception as e:  # noqa: BLE001
            st.session_state["_msg_deshacer"] = f"No se ha podido: {e}"
        st.rerun()
    if c2.button("Cancelar", width="stretch"):
        st.rerun()


def render():
    con = get_con()
    H.init(con)
    st.title("Papelera y deshacer")
    st.caption("Todo lo que se borra o se cambia se puede recuperar desde aquí. Cada acción queda en la auditoría.")
    if st.session_state.get("_msg_deshacer"):
        st.info(st.session_state.pop("_msg_deshacer"))
    if not puede_editar():
        st.warning("Solo los gestores y administradores pueden restaurar o deshacer.")
        return
    tabs = st.tabs(["Documentos en papelera", "Certificaciones, obras y otros", "Subidas", "Lecturas", "Últimas acciones"])

    with tabs[0]:
        pap = db.rows(con, "SELECT id, filename, emisor_nombre, numero, base_imponible_cents/100.0 AS base, estado_previo "
                           "FROM documentos WHERE estado='eliminado' ORDER BY id DESC")
        if not pap:
            st.caption("No hay documentos en la papelera.")
        else:
            st.dataframe(pd.DataFrame(pap), hide_index=True, width="stretch")
            sel = st.multiselect("Documentos", [x["id"] for x in pap], key="pap_sel",
                                 format_func=lambda i: next(f"#{x['id']} · {x['filename']}" for x in pap if x["id"] == i))
            c1, c2 = st.columns(2)
            if c1.button(f"Restaurar {len(sel)}", disabled=not sel, type="primary"):
                for i in sel:
                    ing.restaurar(con, i, usuario())
                st.rerun()
            if c2.button(f"Borrar definitivamente {len(sel)}", disabled=not sel or rol() != "admin",
                         help="Solo administradores. Irreversible."):
                _confirmar(f"Se borrarán **definitivamente** {len(sel)} documento(s). No se podrán recuperar.",
                           lambda ids: [ing.borrar_definitivo(con, i, usuario()) for i in ids] and "", sel)

    with tabs[1]:
        objs = H.objetos(con)
        ETQ = {"certificacion": "Certificación eliminada", "obra": "Obra eliminada",
               "estructura": "Estructura de partidas anterior", "fusion_proveedores": "Fusión de proveedores"}
        if not objs:
            st.caption("Nada que restaurar.")
        for o in objs:
            with st.container(border=True):
                a, b = st.columns([5, 1])
                a.markdown(f"**{ETQ.get(o['tipo'], o['tipo'])}** · {o['descripcion']}  \n:gray[{o['usuario']} · {o['ts'].replace('T', ' ')}]")
                if b.button("Restaurar", key=f"obj_{o['id']}"):
                    _confirmar(f"¿Restaurar «{o['descripcion']}»?", H.restaurar_objeto, con, o["id"], usuario())

    with tabs[2]:
        ls = H.lotes_subida(con)
        if not ls:
            st.caption("Sin subidas registradas.")
        for l_ in ls:
            with st.container(border=True):
                a, b = st.columns([5, 1])
                a.markdown(f"Subida del {str(l_['fecha']).replace('T', ' ')} · **{l_['documentos']}** documento(s)"
                           + (f" · {l_['en_papelera']} ya en la papelera" if l_["en_papelera"] else ""))
                if b.button("Deshacer", key=f"lote_{l_['lote']}", disabled=l_["en_papelera"] == l_["documentos"]):
                    _confirmar(f"Se enviarán a la papelera los {l_['documentos']} documentos de esta subida (restaurables).",
                               H.deshacer_subida, con, l_["lote"], usuario())

    with tabs[3]:
        for t in trabajos.recientes(con, 10):
            with st.container(border=True):
                a, b = st.columns([5, 1])
                a.markdown(f"Lectura nº {t['id']} · {t['estado']} · {t['hechos']}/{t['total']} · {t['creado_por']} · "
                           f"{str(t['creado_en']).replace('T', ' ')}" + (" · **deshecha**" if t.get("mensaje") == "deshecho" else ""))
                if b.button("Deshacer", key=f"lec_{t['id']}", disabled=t["estado"] in ("pendiente", "en_curso")
                            or t.get("mensaje") == "deshecho"):
                    _confirmar("Cada documento de esta lectura volverá a como estaba antes de leerse (se pierden los datos "
                               "leídos, que quedan como versión recuperable).", H.deshacer_lectura, con, t["id"], usuario())

    with tabs[4]:
        aud = pd.DataFrame(db.rows(con, "SELECT ts, usuario, accion, entidad, entidad_id, substr(detalle,1,200) AS detalle "
                                        "FROM auditoria ORDER BY id DESC LIMIT 300"))
        st.dataframe(aud, hide_index=True, width="stretch", height=480)
