"""Contabilidad en SIS: cola de asientos enviados por la API, errores y facturas que cambiaron tras contabilizarse."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from core import db, sis as S, planificador
from vistas.comun import get_con, usuario, rol, ir_a_documento


def render():
    con = get_con()
    st.title("Contabilidad en SIS")
    cfg = S.config(con)
    st.caption({"desactivado": "El envío directo a SIS está DESACTIVADO (se sigue usando el Excel/CSV de asientos).",
                "prueba": "Modo PRUEBA: se genera lo que se enviaría, sin llamar a SIS.",
                "real": f"Envío real a {cfg['url']} cada {cfg['minutos']} min."}[cfg["modo"]])
    res = S.resumen(con)
    k = st.columns(5)
    for i, e in enumerate(["pendiente", "enviado", "error", "fallido", "desfasado"]):
        k[i].metric(S.ESTADOS[e], res.get(e, 0))
    admin = rol() in ("admin", "gestor", "direccion")
    if admin and cfg["modo"] != "desactivado" and st.button("Enviar ahora", icon=":material/upload:"):
        r = planificador.ejecutar_ahora(con, "sis")
        (st.success if r["ok"] else st.error)(r["resultado"] or r["error"])
    filtro = st.multiselect("Estados", list(S.ESTADOS), ["pendiente", "error", "fallido", "desfasado"], format_func=S.ESTADOS.get, key="sis_f")
    filas = db.rows(con, f"""SELECT s.*, d.numero, d.fecha, d.emisor_nombre, d.base_imponible_cents FROM sis_envios s JOIN documentos d ON d.id=s.documento_id
                             WHERE s.estado IN ({','.join('?' * len(filtro)) or "''"}) ORDER BY s.id DESC LIMIT 500""", filtro)
    if not filas:
        st.caption("Nada en estos estados.")
        return
    df = pd.DataFrame(filas)
    df["estado"] = df["estado"].map(S.ESTADOS)
    df["base"] = df["base_imponible_cents"].fillna(0) / 100
    st.dataframe(df[["documento_id", "fecha", "emisor_nombre", "numero", "base", "estado", "referencia", "id_sis", "intentos", "error"]], hide_index=True,
                 width="stretch", column_config={"documento_id": "Doc.", "emisor_nombre": "Proveedor", "numero": "Factura",
                                                 "base": st.column_config.NumberColumn("Base (€)", format="localized"), "referencia": "Referencia",
                                                 "id_sis": "Id en SIS", "error": "Error / motivo"})
    sel = st.selectbox("Revisar", [None] + [f["id"] for f in filas], key="sis_sel",
                       format_func=lambda i: "—" if i is None else next(f"{x['emisor_nombre']} · {x['numero']} · {S.ESTADOS.get(x['estado'], x['estado'])}"
                                                                         for x in filas if x["id"] == i))
    if not sel:
        return
    e = next(x for x in filas if x["id"] == sel)
    if e["payload"]:
        with st.expander("Último cuerpo enviado"):
            st.json(json.loads(e["payload"]))
    try:
        cuerpo, ref, h = S.construir_payload(con, e["documento_id"], cfg)
        with st.expander("Asiento actual de la factura"):
            st.json(cuerpo)
    except ValueError as ex:
        st.warning(str(ex))
    a, b, c = st.columns(3)
    if a.button("Abrir la factura"):
        ir_a_documento(e["documento_id"])
    if admin:
        nota = st.text_input("Nota de la revisión", key="sis_nota")
        if e["estado"] in ("desfasado", "fallido", "error") and b.button("Ya está ajustado en SIS", help="Se da por contabilizada con los importes actuales."):
            S.marcar(con, sel, "enviado", usuario(), nota); st.rerun()
        if e["estado"] in ("pendiente", "error", "fallido") and c.button("Omitir (ya estaba en SIS)"):
            S.marcar(con, sel, "omitido", usuario(), nota); st.rerun()
        if e["estado"] in ("fallido", "desfasado") and c.button("Volver a enviar"):
            S.marcar(con, sel, "pendiente", usuario(), nota); st.rerun()
