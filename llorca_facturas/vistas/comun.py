"""Utilidades compartidas por las páginas Streamlit."""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from core import db, maestros, obra_control, auditoria, usuarios, trabajos, sesiones
from core.config import MODELOS_CLAUDE, ESTADO_LABEL, SEVERIDAD_ICONO

EUR = st.column_config.NumberColumn  # alias corto


@st.cache_resource
def get_con():
    con = db.connect()
    db.init_db(con)
    obra_control.init(con)
    auditoria.init(con)
    usuarios.init(con)
    sesiones.init(con)
    trabajos.init(con)
    from core import ingesta as _ing, plantillas as _pl
    _ing._cols_extra(con)
    _pl.init(con)
    from core import historial as _hi, indice as _ix, gobierno as _gob
    _hi.init(con)
    _ix.init(con)
    _gob.init(con)
    from core import internos as _int, tesoreria as _tes, estudios as _est
    _int.init(con)
    _tes.init(con)
    _est.init(con)
    from core import cierres as _cie, pagos as _pag
    _cie.init(con)
    _pag.init(con)
    con.commit()
    from core import agente as _ag
    _ag.init_chat(con)
    trabajos.arrancar()
    maestros.seed_inicial(con)
    return con


def usuario() -> str:
    a = st.session_state.get("auth")
    return a["usuario"] if a else (st.session_state.get("usuario") or "sin_identificar")


def rol() -> str:
    a = st.session_state.get("auth")
    return a["rol"] if a else "consulta"


def puede_editar() -> bool:
    """Administración de datos (facturas, pagos, maestros)."""
    return rol() in ("admin", "gestor", "direccion")


def puede_aprobar() -> bool:
    return rol() in ("admin", "gestor", "direccion")


def puede_conformar() -> bool:
    return rol() in ("admin", "direccion", "jefe_obra")


def obras_permitidas() -> set | None:
    """None = todas. Un jefe de obra solo ve las obras que tiene asignadas."""
    if rol() != "jefe_obra":
        return None
    a = st.session_state.get("auth") or {}
    return usuarios.obras_de(get_con(), a.get("id"))


def filtro_obras_sql(alias: str = "d") -> tuple[str, list]:
    perm = obras_permitidas()
    if perm is None:
        return "", []
    if not perm:
        return f" AND 1=0", []
    return f" AND {alias}.obra_id IN ({','.join('?' * len(perm))})", list(perm)


def api_key() -> str:
    if st.session_state.get("api_key"):
        return st.session_state["api_key"]
    k = os.environ.get("ANTHROPIC_API_KEY", "")
    if not k:
        try:
            k = st.secrets.get("ANTHROPIC_API_KEY", "")
        except Exception:
            k = ""
    if not k:
        k = db.get_setting(get_con(), "anthropic_api_key", "")
    return k


def modelo() -> str:
    return st.session_state.get("modelo") or db.get_setting(get_con(), "modelo", MODELOS_CLAUDE[0])


def eur_col(label: str, help: str | None = None):
    return st.column_config.NumberColumn(label, format="localized", help=help)


def cents_to_eur(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """Convierte columnas en céntimos a euros SOLO para mostrar (el cálculo ya se hizo en enteros)."""
    out = df.copy()
    for c, nuevo in mapping.items():
        if c in out.columns:
            out[nuevo] = out[c].fillna(0).astype("int64") / 100
    return out.drop(columns=[c for c in mapping if c in out.columns and mapping[c] != c])


def selector_obra(key: str, permitir_todas: bool = True, label: str = "Obra", texto_ninguna: str = "Todas las obras"):
    con = get_con()
    obras = maestros.listar_obras(con, solo_activas=False)
    perm = obras_permitidas()
    if perm is not None:
        obras = [o for o in obras if o["id"] in perm]
        permitir_todas = False
        if not obras:
            st.info("No tiene obras asignadas. Pida al administrador que le asigne sus obras.")
            return None
    opciones = ([None] if permitir_todas else []) + [o["id"] for o in obras]
    nombres = {o["id"]: f"{o['codigo']} · {o['nombre']}" for o in obras}
    if not opciones:
        st.warning("No hay obras. Crea una en «Obras y partidas».")
        return None
    if key in st.session_state and st.session_state[key] not in opciones:
        del st.session_state[key]
    return st.selectbox(label, opciones, key=key,
                        format_func=lambda i: texto_ninguna if i is None else nombres.get(i, str(i)))


def badge_estado(estado: str) -> str:
    colores = {"sin_procesar": "gray", "pendiente_revision": "orange", "revisada": "blue",
               "aprobada": "green", "rechazada": "red"}
    return f":{colores.get(estado, 'gray')}-badge[{ESTADO_LABEL.get(estado, estado)}]"


def icono_sev(sev: str) -> str:
    return SEVERIDAD_ICONO.get(sev, "")


def ir_a_documento(doc_id: int, pagina: int | None = None):
    if pagina:
        st.session_state[f"pag_{int(doc_id)}"] = int(pagina)
    st.session_state["doc_revision"] = int(doc_id)
    st.session_state["rev_sel"] = int(doc_id)
    if "rev_estados" not in st.session_state:
        st.session_state["rev_estados"] = ["pendiente_revision"]
    st.switch_page(st.session_state["_pagina_revision"])
