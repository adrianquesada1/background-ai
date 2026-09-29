"""Pantalla de acceso y de creación del primer administrador."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from core import usuarios as U

LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo_llorca.png"


def pantalla(con) -> None:
    _, centro, _ = st.columns([1, 1.1, 1])
    with centro:
        st.write("")
        st.image(str(LOGO), width=260)
        st.markdown("#### Control económico de obra")
        if not U.hay_usuarios(con):
            st.info("Primera puesta en marcha: cree el usuario administrador.")
            with st.form("primer_admin"):
                u = st.text_input("Usuario", placeholder="nombre.apellido")
                n = st.text_input("Nombre completo")
                c1 = st.text_input("Contraseña", type="password", help="Mínimo 8 caracteres, con letras y números.")
                c2 = st.text_input("Repetir contraseña", type="password")
                if st.form_submit_button("Crear administrador", type="primary", width="stretch"):
                    if c1 != c2:
                        st.error("Las contraseñas no coinciden.")
                    else:
                        try:
                            U.crear(con, u, n or u, c1, "admin", "instalacion")
                            st.success("Administrador creado. Ya puede acceder.")
                        except Exception as e:  # noqa: BLE001
                            st.error(str(e))
            return
        with st.form("login"):
            u = st.text_input("Usuario")
            c = st.text_input("Contraseña", type="password")
            recordar = st.checkbox("Mantener la sesión iniciada en este equipo (30 días)")
            if st.form_submit_button("Acceder", type="primary", width="stretch"):
                ses, err = U.autenticar(con, u, c)
                if ses:
                    from core import sesiones
                    try:
                        nav = st.context.headers.get("User-Agent", "")
                        ip = st.context.ip_address or ""
                    except Exception:
                        nav, ip = "", ""
                    token, seg = sesiones.crear(con, ses["id"], recordar, nav, ip)
                    st.session_state["auth"] = ses
                    st.session_state["usuario"] = ses["usuario"]
                    st.session_state["_token"] = token
                    st.session_state["_cookie_js"] = sesiones.js_cookie(token, seg)
                    st.rerun()
                else:
                    st.error(err)
        st.caption("Acceso restringido al personal autorizado de Llorca Group. Todos los accesos quedan registrados.")
