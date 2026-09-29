"""Bandeja de salida: correos preparados por la aplicación, pendientes de aprobar, enviados y con error."""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from core import db, correo as C, planificador
from vistas.comun import get_con, usuario, rol


def render():
    con = get_con()
    st.title("Correo saliente")
    cfg = C.config(con)
    modo = {"desactivado": "DESACTIVADO: no sale nada", "prueba": "PRUEBA: se generan .eml sin enviar", "real": "Envío real"}[cfg["modo"]]
    st.caption(f"Modo de envío: **{modo}**. Los correos aprobados salen solos en menos de un minuto. "
               "Los tipos marcados como automáticos en «Automatizaciones» no necesitan aprobación.")
    cnt = {r["estado"]: r["n"] for r in db.rows(con, "SELECT estado, COUNT(*) n FROM correo_salida GROUP BY estado")}
    k = st.columns(4)
    k[0].metric("Pendientes de aprobar", cnt.get("pendiente_aprobacion", 0))
    k[1].metric("En cola", cnt.get("aprobado", 0))
    k[2].metric("Con error", cnt.get("error", 0) + cnt.get("fallido", 0))
    k[3].metric("Enviados", cnt.get("enviado", 0))
    if rol() in ("admin", "gestor", "direccion") and st.button("Enviar ahora lo aprobado", icon=":material/send:"):
        r = planificador.ejecutar_ahora(con, "correo")
        (st.success if r["ok"] else st.error)(r["resultado"] or r["error"])
    vista = st.radio("Ver", ["Pendientes de aprobar", "En cola y con error", "Enviados", "Todos"], horizontal=True, key="cs_vista")
    estados = {"Pendientes de aprobar": ["pendiente_aprobacion"], "En cola y con error": ["aprobado", "error", "fallido"],
               "Enviados": ["enviado"], "Todos": None}[vista]
    filas = C.bandeja(con, estados)
    if not filas:
        st.caption("No hay correos en esta vista.")
        return
    for c in filas[:100]:
        with st.container(border=True):
            a, b = st.columns([4, 1.2])
            adj = json.loads(c["adjuntos"] or "[]")
            a.markdown(f"**{c['asunto']}**  \n:gray[{C.TIPOS.get(c['tipo'], c['tipo'])} · para {c['para']}"
                       f"{' · cc ' + c['cc'] if c['cc'] else ''} · preparado por {c['creado_por']} el {str(c['creado_en'])[:16]}]  \n"
                       f"**{C.ESTADOS.get(c['estado'], c['estado'])}**" + (f" · enviado {c['enviado_en'][:16]}" if c["enviado_en"] else "")
                       + (f"  \n:red[{c['error']}]" if c["error"] else ""))
            for p in adj:
                if Path(p).exists():
                    b.download_button(Path(p).name, Path(p).read_bytes(), file_name=Path(p).name, key=f"cs_adj_{c['id']}_{Path(p).name}")
            if c["estado"] in ("pendiente_aprobacion", "error", "fallido"):
                with a.expander("Revisar y aprobar"):
                    para = st.text_input("Para", c["para"], key=f"cs_para_{c['id']}")
                    asunto = st.text_input("Asunto", c["asunto"], key=f"cs_as_{c['id']}")
                    cuerpo = st.text_area("Texto", c["cuerpo"], height=220, key=f"cs_cu_{c['id']}")
                    x, y = st.columns(2)
                    if x.button("Aprobar y enviar", type="primary", key=f"cs_ok_{c['id']}"):
                        try:
                            if c["estado"] == "fallido":
                                con.execute("UPDATE correo_salida SET estado='error' WHERE id=?", (c["id"],))
                            C.aprobar(con, c["id"], usuario(), rol(), {"para": para, "asunto": asunto, "cuerpo": cuerpo})
                            planificador.pedir_ejecucion(con, "correo")
                            st.toast("Aprobado: sale en unos segundos."); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                    if y.button("Cancelar el correo", key=f"cs_no_{c['id']}"):
                        C.cancelar(con, c["id"], usuario()); st.rerun()
            elif c["estado"] == "aprobado" and b.button("Cancelar", key=f"cs_can_{c['id']}"):
                C.cancelar(con, c["id"], usuario()); st.rerun()
            elif c["estado"] == "enviado":
                with a.expander("Ver texto"):
                    st.text(c["cuerpo"])
