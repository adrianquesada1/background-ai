"""Buzón de facturas: lo que ha llegado por correo, qué se ha importado y qué se ha apartado (y por qué)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from core import db, buzon as B, planificador
from vistas.comun import get_con, usuario, selector_obra, puede_editar


def render():
    con = get_con()
    st.title("Buzón de facturas")
    cfg = B.config(con)
    st.caption("Cada pocos minutos se leen los correos nuevos del buzón de facturas. De cada adjunto (PDF, foto, ZIP o correo "
               "reenviado) se decide si es una factura: solo las facturas entran en el circuito y se leen solas. Lo demás queda "
               "apartado con el motivo, por si el clasificador se equivoca.")
    est = next((t for t in planificador.estado(con) if t["nombre"] == "buzon"), {})
    r = B.resumen(con)
    k = st.columns(5)
    k[0].metric("Estado", "Activo" if cfg["activo"] else "Desactivado", help=f"Cada {cfg['minutos']} min · carpeta «{cfg['carpeta']}»")
    k[1].metric("Correos (30 días)", r["mensajes"])
    k[2].metric("Facturas recibidas (30 días)", r["facturas"])
    k[3].metric("Dudosos por decidir", r["dudosos"])
    k[4].metric("Errores", r["errores"])
    if est.get("ultima_ejecucion"):
        (st.error if est.get("ultimo_error") else st.caption)(
            f"Última revisión: {est['ultima_ejecucion'].replace('T', ' ')} · " + (f"ERROR: {est['ultimo_error']}" if est.get("ultimo_error")
                                                                                   else (est.get("ultimo_resultado") or "")))
    if not cfg["activo"]:
        st.info("La lectura automática está desactivada. El administrador la configura en «Automatizaciones → Buzón de facturas».")
    c1, c2 = st.columns([1, 3])
    if puede_editar() and c1.button("Revisar el buzón ahora", icon=":material/sync:", disabled=not cfg["host"]):
        with st.spinner("Leyendo el buzón…"):
            res = planificador.ejecutar_ahora(con, "buzon")
        (st.success if res["ok"] else st.error)(res["resultado"] or res["error"])

    t1, t2, t3 = st.tabs(["Apartados (decidir)", "Correos recibidos", "Remitentes conocidos"])
    with t1:
        _apartados(con)
    with t2:
        _mensajes(con)
    with t3:
        rem = db.rows(con, """SELECT b.email, p.nombre AS proveedor, p.nif, b.veces, b.ultima FROM buzon_remitentes b
                              JOIN proveedores p ON p.id=b.proveedor_id ORDER BY b.veces DESC""")
        st.caption("Aprendidos de las facturas aprobadas que llegaron por correo. Cuando una factura de estos remitentes no trae el CIF "
                   "legible (solo en el logotipo, escaneados), el proveedor se toma de aquí y se avisa para comprobarlo.")
        if rem:
            st.dataframe(pd.DataFrame(rem), hide_index=True, width="stretch",
                         column_config={"email": "Remitente", "proveedor": "Proveedor", "nif": "NIF", "veces": "Facturas", "ultima": "Última"})
        else:
            st.caption("Aún no hay remitentes aprendidos.")


def _apartados(con):
    filtro = st.radio("Mostrar", ["Dudosos", "No son factura", "Errores"], horizontal=True, key="buz_filtro")
    clase = {"Dudosos": "dudoso", "No son factura": "no_factura", "Errores": "error"}[filtro]
    filas = db.rows(con, """SELECT a.*, m.remitente, m.remitente_email, m.asunto, m.fecha FROM buzon_adjuntos a JOIN buzon_mensajes m ON m.id=a.mensaje_id
                            WHERE a.clasificacion=? AND a.documento_id IS NULL ORDER BY a.id DESC LIMIT 200""", (clase,))
    if not filas:
        st.caption("Nada apartado en esta categoría.")
        return
    obra_id = selector_obra("buz_obra", texto_ninguna="Detectar la obra de cada factura") if puede_editar() else None
    for a in filas:
        with st.container(border=True):
            x, y = st.columns([4, 1.4])
            mot = json.loads(a["motivos"] or "[]")
            x.markdown(f"**{a['nombre']}** · de {a['remitente'] or ''} <{a['remitente_email']}> · {str(a['fecha'] or '')[:16]}  \n"
                       f":gray[Asunto: {a['asunto'] or ''}]  \n:gray[Puntuación {a['puntuacion']}: {'; '.join(mot)}]")
            if a["ruta"] and Path(a["ruta"]).exists():
                y.download_button("Ver PDF", Path(a["ruta"]).read_bytes(), file_name=a["nombre"], key=f"bz_v_{a['id']}", mime="application/pdf")
            if puede_editar() and clase != "error":
                if y.button("Es factura: importar", key=f"bz_i_{a['id']}", type="primary"):
                    try:
                        did = B.importar_apartado(con, a["id"], usuario(), obra_id)
                        st.toast(f"Importada como documento #{did} y encolada para su lectura."); st.rerun()
                    except ValueError as e:
                        st.error(str(e))
                if clase == "dudoso" and y.button("No es factura", key=f"bz_d_{a['id']}"):
                    B.descartar_apartado(con, a["id"], usuario()); st.rerun()


def _mensajes(con):
    q = st.text_input("Buscar (remitente, asunto)", key="buz_q")
    filas = db.rows(con, "SELECT * FROM buzon_mensajes ORDER BY id DESC LIMIT 500")
    if q:
        filas = [f for f in filas if q.lower() in f"{f['remitente']} {f['remitente_email']} {f['asunto']}".lower()]
    if not filas:
        st.caption("Aún no ha llegado ningún correo.")
        return
    df = pd.DataFrame(filas)
    df["estado"] = df["estado"].map(lambda e: B.ESTADOS.get(e, e))
    st.dataframe(df[["recibido_en", "remitente_email", "asunto", "n_adjuntos", "n_facturas", "estado", "error"]], hide_index=True, width="stretch",
                 column_config={"recibido_en": "Leído", "remitente_email": "Remitente", "asunto": "Asunto", "n_adjuntos": "Adjuntos",
                                "n_facturas": "Facturas", "estado": "Estado", "error": "Error"})
    sel = st.selectbox("Ver detalle del correo", [None] + [f["id"] for f in filas[:200]], key="buz_sel",
                       format_func=lambda i: "—" if i is None else next(f"{x['remitente_email']} · {x['asunto'] or ''}"[:90] for x in filas if x["id"] == i))
    if sel:
        adj = db.rows(con, "SELECT nombre, clasificacion, puntuacion, motivos, documento_id FROM buzon_adjuntos WHERE mensaje_id=?", (sel,))
        for a in adj:
            a["clasificacion"] = B.CLASES.get(a["clasificacion"], a["clasificacion"])
            a["motivos"] = "; ".join(json.loads(a["motivos"] or "[]"))
        st.dataframe(pd.DataFrame(adj), hide_index=True, width="stretch",
                     column_config={"nombre": "Adjunto", "clasificacion": "Clasificación", "puntuacion": "Puntos", "motivos": "Motivos",
                                    "documento_id": "Documento"})
