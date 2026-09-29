"""Asistente conversacional: preguntas en lenguaje natural sobre obras, partidas, proveedores y facturas."""
from __future__ import annotations

import json

import streamlit as st

from core import agente, db, lector
from core.extractor_local import ollama_disponible
import pandas as pd
from vistas.comun import get_con, api_key, modelo, usuario

EJEMPLOS = [
    "¿Cuánto llevamos gastado en la obra 664 y en qué partidas?",
    "¿Qué proveedores concentran el 80 % del coste?",
    "¿Cuánto hemos pagado en papel pintado y a quién?",
    "¿Cuánta retención de garantía tenemos que devolver y cuándo?",
    "¿Hay facturas duplicadas o cambios de IBAN?",
    "¿Qué facturas están vencidas sin pagar?",
]


def render():
    con = get_con()
    st.title("Asistente IA")
    st.caption("Las cifras las calcula el sistema con aritmética exacta; la IA solo interpreta la pregunta y redacta. "
               "Cada respuesta muestra las consultas que ha hecho.")
    cfg = lector.config(con)
    from core.ollama_cliente import elegir_modelo_razonamiento
    cfg["ollama_modelo"] = elegir_modelo_razonamiento(cfg["ollama_url"], cfg["ollama_razonamiento_modelo"]) or cfg["ollama_razonamiento_modelo"]
    local_ok = cfg["ollama_modelo"] in ollama_disponible(cfg["ollama_url"])
    usar_nube = cfg["motor"] == "claude" and bool(api_key())
    if not usar_nube and not local_ok:
        st.info(f"Para conversar en lenguaje natural sin salir del equipo, instala Ollama y el modelo «{cfg['ollama_modelo']}» "
                "(ver  Configuración). Mientras tanto, estas consultas funcionan sin ninguna IA:")
        _consultas_directas(con)
        return
    st.caption(" Modelo local: " + cfg["ollama_modelo"] if not usar_nube else " Claude (API)")
    agente.init_chat(con)
    u = usuario()
    c1, c2 = st.columns([5, 1])
    redactar = c1.toggle("Redactar las respuestas con la IA local", value=False, key="chat_redactar",
                         help="Desactivado: respuesta al instante con las cifras exactas del sistema. Activado: el modelo local "
                              "redacta la respuesta (en CPU tarda entre 30 s y 2 min).") if not usar_nube else False
    if c2.button("Nueva conversación", icon=":material/add_comment:"):
        agente.nueva_conversacion(con, u)
        st.rerun()
    historial_ui = [m for m in agente.mensajes(con, u) if m["role"] in ("user", "assistant")]

    if not historial_ui:
        st.markdown("**Pruebe con:**")
        cols = st.columns(3)
        for k, e in enumerate(EJEMPLOS):
            if cols[k % 3].button(e, key=f"ej_{k}", width="stretch"):
                st.session_state["pregunta_pendiente"] = e
                st.rerun()

    for m in historial_ui:
        with st.chat_message(m["role"]):
            st.markdown(m["texto"])
            if m.get("traza"):
                with st.expander(f"{len(m['traza'])} consulta(s) a los datos"):
                    for t in m["traza"]:
                        st.markdown(f"**{t['herramienta']}** `{json.dumps(t['parametros'], ensure_ascii=False)}`")
                        st.json(t["resultado"], expanded=False)

    pregunta = st.chat_input("Pregunte sobre obras, partidas, proveedores, facturas, certificaciones…") \
        or st.session_state.pop("pregunta_pendiente", None)
    if pregunta:
        agente.guardar_mensaje(con, u, "user", pregunta)
        with st.chat_message("user"):
            st.markdown(pregunta)
        with st.chat_message("assistant"):
            with st.spinner("Consultando los datos…"):
                hist = [{"role": m["role"], "content": m["texto"]} for m in historial_ui[-10:]] + [{"role": "user", "content": pregunta}]

                def _redactar_local(preg, traza_):
                    from core.ollama_cliente import chat
                    datos = json.dumps([{"consulta": t_["herramienta"], "resultado": t_["resultado"]} for t_ in traza_],
                                       ensure_ascii=False, default=str)[:9000]
                    m_ = chat(cfg["ollama_url"], cfg["ollama_modelo"], [
                        {"role": "system", "content": "Responde en español, breve y claro, usando SOLO las cifras de los datos. "
                                                      "No calcules nada nuevo. Usa una tabla si hay varias filas."},
                        {"role": "user", "content": f"Pregunta: {preg}\n\nDatos:\n{datos}"}],
                        opciones={**lector.opciones_ollama(cfg), "num_ctx": 4096, "num_predict": 400}, timeout=120)
                    return m_.get("content") or agente.redactar_sin_ia(traza_)
                try:
                    rapida = agente.responder_rapido(con, pregunta, hist,
                                                     _redactar_local if (redactar and not usar_nube) else None)
                except Exception:
                    rapida = None
                try:
                    if rapida:
                        texto, traza, _ = rapida
                    elif usar_nube:
                        texto, traza, _ = agente.responder(con, api_key(), modelo(), hist)
                    else:
                        texto, traza, _ = agente.responder_ollama(con, cfg["ollama_url"], cfg["ollama_modelo"], hist,
                                                                  opciones=lector.opciones_ollama(cfg))
                except Exception as e:  # noqa: BLE001
                    texto, traza = f"No se ha podido consultar la IA: {e}", []
            st.markdown(texto)
            if traza:
                with st.expander(f"{len(traza)} consulta(s) a los datos"):
                    for t in traza:
                        st.markdown(f"**{t['herramienta']}** `{json.dumps(t['parametros'], ensure_ascii=False)}`")
                        st.json(t["resultado"], expanded=False)
        agente.guardar_mensaje(con, u, "assistant", texto, traza)
        db.audit(con, u, "pregunta_asistente", "chat", None, {"pregunta": pregunta, "consultas": len(traza)})
        con.commit()


def _corte_valido(h: list) -> bool:
    """El historial recortado debe empezar por un mensaje de usuario con texto (no un tool_result)."""
    return bool(h) and h[0]["role"] == "user" and isinstance(h[0]["content"], str)


def _consultas_directas(con):
    obras = db.rows(con, "SELECT codigo, nombre FROM obras ORDER BY codigo")
    if not obras:
        return
    obra = st.selectbox("Obra", [o["codigo"] for o in obras],
                        format_func=lambda c: next(f"{o['codigo']} · {o['nombre']}" for o in obras if o["codigo"] == c))
    opciones = {
        "Rentabilidad por capítulo (mes)": ("rentabilidad_capitulos", {"obra": obra, "modo": "mes"}, "capitulos"),
        "Coste por partida": ("coste_por_partida", {"obra": obra}, "partidas"),
        "Proveedores (Pareto)": ("coste_por_proveedor", {"obra": obra}, "proveedores"),
        "Retenciones de garantía": ("retenciones_garantia", {"obra": obra}, "detalle"),
        "Pagos pendientes": ("pagos_pendientes", {"obra": obra}, "vencidas"),
        "Incidencias abiertas": ("incidencias_abiertas", {"obra": obra}, "incidencias"),
        "Ofertas y contratos": ("ofertas_contratos", {"obra": obra}, "ofertas"),
    }
    q = st.radio("Consulta", list(opciones), horizontal=True)
    texto = st.text_input("Buscar concepto en facturas (p.ej. «papel», «gres»)")
    if texto:
        out = agente.ejecutar_herramienta(con, "buscar_lineas", {"obra": obra, "texto": texto})
        st.metric("Suma", out.get("suma", "—"))
        st.dataframe(pd.DataFrame(out.get("lineas", [])), hide_index=True, width="stretch")
        return
    t, a, clave = opciones[q]
    out = agente.ejecutar_herramienta(con, t, a)
    resumen = {k: v for k, v in out.items() if not isinstance(v, (list, dict))}
    if resumen:
        st.json(resumen, expanded=True)
    st.dataframe(pd.DataFrame(out.get(clave, [])), hide_index=True, width="stretch")
