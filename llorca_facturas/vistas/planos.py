"""Planos y fichas técnicas: versiones, aprobación de la dirección facultativa y distribución."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from core import planos as PN
from vistas.comun import get_con, usuario, rol, selector_obra


def render():
    con = get_con()
    st.title("Planos y fichas técnicas")
    st.caption("La vigente es siempre la última revisión APROBADA por la dirección facultativa. Si llega una revisión posterior sin aprobar, "
               "se avisa: en obra se sigue trabajando con la vigente. Al aprobar una nueva, la anterior pasa a «superada» y se avisa de "
               "las copias entregadas que hay que retirar.")
    obra_id = selector_obra("pn_obra", permitir_todas=False)
    if not obra_id:
        return
    edita = rol() in ("admin", "gestor", "direccion", "jefe_obra", "tecnico")
    lista = PN.listado(con, obra_id)
    pend = PN.pendientes_df(con, obra_id)
    retirar = PN.entregas_a_retirar(con, obra_id)
    k = st.columns(4)
    k[0].metric("Documentos", len(lista))
    k[1].metric("Pendientes de la DF", sum(x["pendientes_df"] for x in lista))
    k[2].metric("Sin aprobar", sum(1 for x in lista if not x["vigente"]))
    k[3].metric("Copias a retirar", len(retirar))
    for r in pend:
        st.warning(f"{r['codigo']} rev. {r['revision']} lleva desde el {r['enviado_df_en'][:10]} sin respuesta de la DF.")
    t1, t2, t3 = st.tabs(["Listado", "Subir revisión", "Documento"])
    with t1:
        if lista:
            df = pd.DataFrame(lista)
            df["tipo"] = df["tipo"].map(PN.TIPOS)
            df["ultima_estado"] = df["ultima_estado"].map(lambda e: PN.ESTADOS.get(e, e))
            q = st.text_input("Buscar", key="pn_q")
            if q:
                df = df[df.apply(lambda r: q.lower() in f"{r['codigo']} {r['titulo']} {r['disciplina']} {r['afecta_a']}".lower(), axis=1)]
            st.dataframe(df[["codigo", "titulo", "tipo", "disciplina", "vigente", "vigente_fecha", "aprobado_por_df", "ultima", "ultima_estado", "aviso"]],
                         hide_index=True, width="stretch",
                         column_config={"codigo": "Código", "titulo": "Título", "tipo": "Tipo", "disciplina": "Disciplina", "vigente": "Rev. vigente",
                                        "vigente_fecha": "Aprobada", "aprobado_por_df": "Aprobó (DF)", "ultima": "Última rev.",
                                        "ultima_estado": "Estado de la última", "aviso": "Aviso"})
        else:
            st.caption("Aún no hay documentos.")
        if retirar:
            st.markdown("**Copias entregadas de revisiones superadas o rechazadas (retirarlas de obra)**")
            for x in retirar:
                a, b = st.columns([4, 1])
                a.markdown(f"{x['codigo']} rev. {x['revision']} ({PN.ESTADOS[x['estado']].lower()}) · entregada a **{x['destinatario']}** el {x['fecha']}")
                if edita and b.button("Retirada", key=f"pn_ret_{x['id']}"):
                    PN.marcar_retirada(con, x["id"], usuario()); st.rerun()
    with t2:
        if not edita:
            st.caption("Solo lectura.")
        else:
            up = st.file_uploader("Archivo (PDF, DWG, imagen…)", key=f"pn_up_{obra_id}")
            cod_prop, rev_prop = PN.proponer_desde_nombre(up.name) if up else ("", None)
            existentes = {x["codigo"].lower(): x for x in lista}
            with st.form(f"pn_form_{obra_id}", clear_on_submit=True):
                c = st.columns([1.3, 2.5, 0.8])
                cod = c[0].text_input("Código", cod_prop)
                tit = c[1].text_input("Título (si es un documento nuevo)", existentes.get(cod_prop.lower(), {}).get("titulo", ""))
                rev = c[2].text_input("Revisión", rev_prop or "")
                c = st.columns(3)
                tipo = c[0].selectbox("Tipo", list(PN.TIPOS), format_func=PN.TIPOS.get)
                disc = c[1].selectbox("Disciplina", PN.DISCIPLINAS)
                afecta = c[2].text_input("Afecta a (subcontrata, material…)")
                cambios = st.text_area("Qué cambia en esta revisión", height=70)
                enviar = st.checkbox("Enviar ya a la dirección facultativa", True)
                if st.form_submit_button("Registrar revisión", type="primary"):
                    if not up:
                        st.error("Seleccione el archivo.")
                    else:
                        try:
                            ex = existentes.get(cod.strip().lower())
                            did = ex["id"] if ex else PN.crear_documento(con, obra_id, cod, tit or cod, tipo, disc, afecta, usuario())
                            rid = PN.subir_revision(con, did, rev, up.name, up.getvalue(), usuario(), date.today().isoformat(), cambios)
                            if enviar:
                                PN.enviar_df(con, rid, usuario())
                            st.success(f"{cod} rev. {rev.upper()} registrada."); st.rerun()
                        except ValueError as e:
                            st.error(str(e))
    with t3:
        if not lista:
            return
        did = st.selectbox("Documento", [x["id"] for x in lista], key="pn_doc",
                           format_func=lambda i: next(f"{x['codigo']} · {x['titulo']}" for x in lista if x["id"] == i))
        for r in PN.revisiones(con, did):
            with st.container(border=True):
                a, b = st.columns([4, 1.4])
                a.markdown(f"**Rev. {r['revision']}** · {r['fecha']} · **{PN.ESTADOS[r['estado']]}**"
                           + (f" por {r['aprobado_por_df']} el {str(r['resuelto_en'])[:10]}" if r["aprobado_por_df"] else "")
                           + (f"  \n:gray[Cambios: {r['descripcion_cambios']}]" if r["descripcion_cambios"] else "")
                           + (f"  \n:orange[Comentarios DF: {r['comentarios_df']}]" if r["comentarios_df"] else ""))
                if r["archivo"] and Path(r["archivo"]).exists():
                    b.download_button("Descargar", Path(r["archivo"]).read_bytes(), file_name=r["nombre_archivo"] or Path(r["archivo"]).name, key=f"pn_dl_{r['id']}")
                if r["justificante"] and Path(r["justificante"]).exists():
                    b.download_button("Justificante DF", Path(r["justificante"]).read_bytes(), file_name=Path(r["justificante"]).name, key=f"pn_js_{r['id']}")
                if not edita:
                    continue
                if r["estado"] == "borrador" and b.button("Enviar a la DF", key=f"pn_env_{r['id']}"):
                    PN.enviar_df(con, r["id"], usuario()); st.rerun()
                if r["estado"] in ("borrador", "enviada_df"):
                    with a.expander("Registrar la respuesta de la dirección facultativa"):
                        res = st.radio("Resultado", ["aprobada", "aprobada_comentarios", "rechazada"], format_func=PN.ESTADOS.get, horizontal=True,
                                       key=f"pn_res_{r['id']}")
                        quien = st.text_input("Quién de la DF", key=f"pn_q_{r['id']}")
                        com = st.text_area("Comentarios", key=f"pn_c_{r['id']}", height=70)
                        just = st.file_uploader("Justificante (acta, correo, plano sellado)", key=f"pn_j_{r['id']}")
                        if st.button("Guardar respuesta", key=f"pn_g_{r['id']}", type="primary"):
                            try:
                                PN.resolver_df(con, r["id"], res, quien, com, usuario(), just.getvalue() if just else None, just.name if just else "")
                                st.rerun()
                            except ValueError as e:
                                st.error(str(e))
                if r["estado"] in PN.APROBADAS:
                    with a.expander("Registrar entrega"):
                        dest = st.text_input("Entregado a (separe con comas)", key=f"pn_d_{r['id']}")
                        medio = st.selectbox("Medio", ["correo", "papel", "plataforma"], key=f"pn_m_{r['id']}")
                        if st.button("Registrar", key=f"pn_dr_{r['id']}"):
                            PN.distribuir(con, r["id"], dest.split(","), medio, usuario()); st.rerun()
