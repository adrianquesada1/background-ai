"""Revisión humana: PDF a la izquierda, datos a la derecha. La IA propone, la persona decide."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from core import db, maestros, ingesta as ing
from core.config import TIPOS_DOCUMENTO, TIPOS_LINEA, ESTADO_LABEL
from core.extractor import ExtractionError
from core import lector
from core.fiscal import format_iban, validate_iban, validate_nif
from core.money import from_cents, fmt_eur, parse_amount, D
from core.pdf_utils import render_pages
from core.validation import cargar_modelo, resumen_cuadre
from vistas.comun import (get_con, usuario, api_key, modelo, badge_estado, icono_sev, rol, puede_aprobar,
                          puede_conformar, obras_permitidas, filtro_obras_sql)


@st.cache_data(show_spinner=False, max_entries=64)
def _paginas(path: str, n: int):
    return render_pages(Path(path).read_bytes(), max_pages=n)


def _es(v) -> str:
    """Decimal/str máquina -> texto editable en formato español, sin separador de miles (sin ambigüedad)."""
    if v is None or v == "":
        return ""
    return format(D(v), "f").replace(".", ",")


def _e_cents(c) -> str:
    return "" if c is None else _es(from_cents(c))


def _parse(s):
    s = (s or "").strip()
    return None if s == "" else parse_amount(s)


def _cola(con):
    c1, c2 = st.columns([1, 3])
    if "rev_estados" not in st.session_state:
        st.session_state["rev_estados"] = ["pendiente_revision"]
    estados = c1.multiselect("Estados", list(ESTADO_LABEL),
                             format_func=lambda e: ESTADO_LABEL[e], key="rev_estados")
    if not estados:
        estados = list(ESTADO_LABEL)
    docs = db.rows(con, f"""
        SELECT d.id, d.filename, d.numero, d.emisor_nombre, d.estado, d.base_imponible_cents,
          (SELECT COUNT(*) FROM incidencias i WHERE i.documento_id=d.id AND i.resuelta=0 AND i.severidad='critica') AS cr,
          (SELECT COUNT(*) FROM incidencias i WHERE i.documento_id=d.id AND i.resuelta=0 AND i.severidad='alta') AS al
        FROM documentos d WHERE d.estado IN ({','.join('?'*len(estados))}){filtro_obras_sql()[0]}
        ORDER BY cr DESC, al DESC, d.id""", estados + filtro_obras_sql()[1])
    if not docs:
        c2.info("No hay documentos en esos estados.")
        return None, []
    ids = [d["id"] for d in docs]
    lab = {d["id"]: f"#{d['id']} · {d['emisor_nombre'] or d['filename']} · {d['numero'] or 's/n'} · "
                    f"{fmt_eur(from_cents(d['base_imponible_cents'] or 0))}"
                    f"{' · con incidencias críticas' * bool(d['cr'])}{' · con incidencias altas' * bool(d['al'] and not d['cr'])}" for d in docs}
    # la selección vive en una única clave de estado: los botones Anterior/Siguiente la cambian ANTES de pintar el selector
    if st.session_state.get("doc_revision") in ids:
        st.session_state["rev_sel"] = st.session_state.pop("doc_revision")
    if st.session_state.get("rev_sel") not in ids:
        st.session_state["rev_sel"] = ids[0]
    sel = c2.selectbox(f"Documento ({len(ids)} en cola)", ids, format_func=lambda i: lab[i], key="rev_sel")
    return sel, ids


def render():
    con = get_con()
    st.title("Revisión y aprobación")
    limpios = db.one(con, """SELECT COUNT(*) n FROM documentos d WHERE d.estado IN ('pendiente_revision','revisada')
        AND NOT EXISTS (SELECT 1 FROM incidencias i WHERE i.documento_id=d.id AND i.resuelta=0 AND i.severidad<>'info')""")["n"]
    if limpios:
        with st.expander(f" Modo rápido: {limpios} documento(s) han superado TODOS los controles"):
            st.caption("Aprobación masiva solo de documentos sin ninguna incidencia abierta (crítica, alta o media). "
                       "Queda firmada con tu usuario y se puede deshacer documento a documento.")
            ok = st.checkbox("He revisado el listado y apruebo estos documentos")
            if st.button(f"Aprobar {limpios} documento(s)", disabled=not ok, type="primary"):
                n = ing.aprobar_sin_incidencias(con, usuario(), rol=rol())
                st.toast(f"{n} aprobados"); st.rerun()
    doc_id, ids = _cola(con)
    if not doc_id:
        return
    doc = cargar_modelo(con, doc_id)
    from core import historial as _H
    import hashlib as _hl
    import json as _js
    huella_ahora = _hl.sha1(_js.dumps(_H.foto(con, doc_id), default=str, sort_keys=True).encode()).hexdigest()
    clave_h = f"_huella_{doc_id}"
    previa = st.session_state.get(clave_h)          # lo que el usuario tenía en pantalla (ejecución anterior)
    st.session_state[clave_h] = huella_ahora         # lo que verá ahora

    from core.cierres import cerrado as _mes_cerrado
    bloqueado = _mes_cerrado(con, doc.get("obra_id"), doc.get("fecha"))
    if bloqueado:
        st.warning(f"El mes {str(doc.get('fecha'))[:7]} de esta obra está CERRADO: el documento es de solo lectura. "
                   "Para corregirlo, Dirección debe reabrir el mes en «Cierre mensual».")

    def _sin_conflicto() -> bool:
        if bloqueado:
            st.error("Mes cerrado: no se guarda nada.")
            return False
        """True si nadie más ha cambiado el documento desde que se mostró en esta pantalla."""
        if previa and previa != huella_ahora:
            ult = db.one(con, "SELECT usuario, ts FROM auditoria WHERE entidad='documento' AND entidad_id=? ORDER BY id DESC LIMIT 1",
                         (doc_id,))
            st.error(f"Otro usuario ({ult and ult['usuario']}, {ult and ult['ts']}) ha modificado este documento mientras lo tenía "
                     "abierto. Para no pisar sus cambios, no se ha guardado: revise los datos actuales y repita el cambio.")
            return False
        return True

    def _refrescar_huella():
        pass

    nav1, nav2, nav3 = st.columns([1, 1, 6])
    i = ids.index(doc_id)

    def _ir(j):
        st.session_state["rev_sel"] = ids[j]
    nav1.button("Anterior", icon=":material/chevron_left:", disabled=i == 0, on_click=_ir, args=(max(0, i - 1),),
                width="stretch")
    nav2.button("Siguiente", icon=":material/chevron_right:", disabled=i == len(ids) - 1, on_click=_ir,
                args=(min(len(ids) - 1, i + 1),), width="stretch")
    nav3.caption(f"Documento {i + 1} de {len(ids)}")

    col_pdf, col_dat = st.columns([5, 6], gap="large")

    # ================================================================ PDF
    with col_pdf:
        st.markdown(f"**{doc['filename']}** · {doc['paginas'] or '?'} pág. · "
                    f"{'texto' if doc['tiene_texto'] else 'escaneado'}")
        n = min(doc["paginas"] or 1, 30)
        qd = st.text_input("Buscar en este documento", key=f"qdoc_{doc_id}", placeholder="texto, importe, nº de albarán…")
        if qd and len(qd) >= 2:
            pags = db.rows(con, "SELECT pagina, texto FROM doc_paginas WHERE documento_id=? ORDER BY pagina", (doc_id,))
            import unicodedata as _ud
            def _n(t):
                return "".join(c for c in _ud.normalize("NFKD", (t or "").lower()) if not _ud.combining(c))
            hits = [(p["pagina"], _n(p["texto"]).count(_n(qd))) for p in pags if _n(qd) in _n(p["texto"])]
            if hits:
                st.caption("Aparece en: " + ", ".join(f"pág. {p_} ({c_})" for p_, c_ in hits))
                if hits[0][0] <= n and st.session_state.get(f"pag_{doc_id}") not in [h[0] for h in hits]:
                    st.session_state[f"pag_{doc_id}"] = hits[0][0]
            else:
                st.caption("No aparece en el texto de este documento.")
        if st.session_state.get(f"pag_{doc_id}", 1) > max(1, n):
            st.session_state[f"pag_{doc_id}"] = 1
        pag = (st.number_input("Página", 1, max(1, n), key=f"pag_{doc_id}") if f"pag_{doc_id}" in st.session_state
               else st.number_input("Página", 1, max(1, n), 1, key=f"pag_{doc_id}")) if n > 1 else 1
        if not doc["file_path"] or not Path(doc["file_path"]).exists():
            st.info("Documento registrado a mano, sin PDF.")
        else:
            try:
                imgs = _paginas(doc["file_path"], n)
                st.image(imgs[pag - 1], width="stretch")
            except Exception as e:  # noqa: BLE001
                st.error(f"No se pudo renderizar el PDF: {e}")
            st.download_button("Descargar PDF original", Path(doc["file_path"]).read_bytes(), file_name=doc["filename"],
                               mime="application/pdf", key=f"dl_{doc_id}")

    # ================================================================ DATOS
    with col_dat:
        st.markdown(f"{badge_estado(doc['estado'])} &nbsp; **{doc['emisor_nombre'] or 'Emisor sin identificar'}** · "
                    f"nº {doc['numero'] or '—'} · {doc['fecha'] or 'sin fecha'}")
        if doc.get("modelo"):
            st.caption(f"Leído por {doc['modelo']} el {doc.get('extraido_en') or '—'} · confianza "
                       f"{(doc.get('confianza') or 0):.0%}")
        if doc["estado"] == "sin_procesar":
            st.warning("Este documento aún no se ha leído. Léelo con IA o rellénalo a mano en la cabecera.")

        # ------------------------------------------------ incidencias
        inc = db.rows(con, "SELECT * FROM incidencias WHERE documento_id=? ORDER BY resuelta, "
                           "CASE severidad WHEN 'critica' THEN 0 WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END", (doc_id,))
        abiertas = [x for x in inc if not x["resuelta"]]
        with st.expander(f"Incidencias ({len(abiertas)} abiertas de {len(inc)})", expanded=bool(abiertas)):
            if not inc:
                st.success("Sin incidencias: todos los controles superados.")
            for x in inc:
                cab = f"{icono_sev(x['severidad'])} **{x['mensaje']}**" + (" · resuelta" if x["resuelta"] else "")
                st.markdown(cab)
                if x["detalle"]:
                    st.caption(x["detalle"])
                if x["resuelta"]:
                    cz1, cz2 = st.columns([5, 1])
                    cz1.caption(f"Resuelta por {x['resuelta_por']} ({x['resuelta_en']}): {x['comentario'] or ''}")
                    if cz2.button("Reabrir", key=f"reab_{x['id']}"):
                        ing.reabrir_incidencia(con, x["id"], usuario()); st.rerun()
                elif x["severidad"] != "info":
                    cc1, cc2 = st.columns([4, 1])
                    com = cc1.text_input("Justificación", key=f"com_{x['id']}", label_visibility="collapsed",
                                         placeholder="Justificación para dar por buena (obligatoria)")
                    if cc2.button("Resolver", key=f"res_{x['id']}", disabled=not com.strip()):
                        ing.resolver_incidencia(con, x["id"], usuario(), com.strip()); st.rerun()

        st.dataframe(pd.DataFrame(resumen_cuadre(doc)), hide_index=True, width="stretch")

        t_cab, t_lin, t_iva, t_hist = st.tabs(["Cabecera", "Líneas y partidas", "IVA", "Historial"])

        # ------------------------------------------------ cabecera
        with t_cab:
            obras = maestros.listar_obras(con, solo_activas=False)
            obra_ids = [None] + [o["id"] for o in obras]
            with st.form(f"cab_{doc_id}"):
                a1, a2, a3 = st.columns(3)
                tipo = a1.selectbox("Tipo", TIPOS_DOCUMENTO, index=TIPOS_DOCUMENTO.index(doc["tipo_documento"] or "factura"))
                obra_id = a2.selectbox("Obra", obra_ids, index=obra_ids.index(doc["obra_id"]) if doc["obra_id"] in obra_ids else 0,
                                       format_func=lambda x: "— sin obra —" if x is None else
                                       next(f"{o['codigo']} · {o['nombre']}" for o in obras if o["id"] == x))
                a3.text_input("Referencia de obra en el documento", doc["referencia_obra_texto"] or "", disabled=True)
                b1, b2, b3 = st.columns([2, 1, 1])
                emisor = b1.text_input("Emisor", doc["emisor_nombre"] or "")
                nif = b2.text_input("NIF emisor", doc["emisor_nif"] or "")
                rnif = b3.text_input("NIF receptor", doc["receptor_nif"] or "")
                c1, c2, c3, c4 = st.columns(4)
                numero = c1.text_input("Nº factura", doc["numero"] or "")
                fecha = c2.date_input("Fecha", datetime.strptime(doc["fecha"], "%Y-%m-%d").date() if doc["fecha"] else None,
                                      format="DD/MM/YYYY")
                venc = c3.date_input("Vencimiento", datetime.strptime(doc["fecha_vencimiento"], "%Y-%m-%d").date()
                                     if doc["fecha_vencimiento"] else None, format="DD/MM/YYYY")
                rect = c4.text_input("Rectifica a (abonos)", doc["factura_rectificada"] or "")
                st.caption("Importes en formato español sin puntos de miles (p. ej. 15000,00). Se guardan en céntimos exactos.")
                d1, d2, d3 = st.columns(3)
                bruto = d1.text_input("Importe bruto", _e_cents(doc["importe_bruto_cents"]))
                base = d2.text_input("Base imponible", _e_cents(doc["base_imponible_cents"]))
                tfac = d3.text_input("Total factura (base+IVA)", _e_cents(doc["total_factura_cents"]))
                e1, e2, e3, e4, e5 = st.columns(5)
                rpct = e1.text_input("% ret. garantía", _es(doc["ret_garantia_pct"]))
                rbase = e2.text_input("Base retención", _e_cents(doc["ret_garantia_base_cents"]))
                rimp = e3.text_input("Ret. garantía", _e_cents(doc["ret_garantia_cents"]))
                ipct = e4.text_input("% IRPF", _es(doc["irpf_pct"]))
                iimp = e5.text_input("IRPF", _e_cents(doc["irpf_cents"]))
                g1, g2, g3 = st.columns(3)
                tpag = g1.text_input("Total a pagar", _e_cents(doc["total_a_pagar_cents"]))
                isp = g2.checkbox("Inversión del sujeto pasivo", bool(doc["inversion_sujeto_pasivo"]))
                exen = g3.text_input("Motivo exención", doc["exencion_motivo"] or "")
                h1, h2, h3 = st.columns([2, 1, 2])
                iban = h1.text_input("IBAN de pago", format_iban(doc["iban"]))
                fpago = h2.text_input("Forma de pago", doc["forma_pago"] or "")
                ctas = [None] + list(ing.CUENTAS_PGC)
                cuenta = h3.selectbox("Cuenta contable", ctas, index=ctas.index(doc.get("cuenta_contable")) if doc.get("cuenta_contable") in ctas else 0,
                                      format_func=lambda c: "— sin asignar —" if c is None else ing.CUENTAS_PGC[c],
                                      help="Propuesta automática: la habitual del proveedor o, si es nuevo, según el contenido.")
                notas = st.text_area("Notas internas", doc["notas"] or "", height=68)
                if st.form_submit_button("Guardar cabecera y revalidar", type="primary") and _sin_conflicto():
                    malos = [n for n, v in (("Importe bruto", bruto), ("Base imponible", base), ("Total factura", tfac),
                                            ("% ret. garantía", rpct), ("Base retención", rbase), ("Ret. garantía", rimp),
                                            ("% IRPF", ipct), ("IRPF", iimp), ("Total a pagar", tpag))
                             if (v or "").strip() and not re.fullmatch(r"-?[\d.,\s]+€?", v.strip())]
                    if malos:
                        st.error("Estos importes no son números válidos: " + ", ".join(malos) + ". No se ha guardado nada.")
                        st.stop()
                    cambios = {
                        "tipo_documento": tipo, "obra_id": obra_id, "emisor_nombre": emisor or None, "emisor_nif": nif,
                        "receptor_nif": rnif, "numero": numero or None,
                        "fecha": fecha.isoformat() if fecha else None,
                        "fecha_vencimiento": venc.isoformat() if venc else None, "factura_rectificada": rect or None,
                        "importe_bruto_cents": _parse(bruto), "base_imponible_cents": _parse(base),
                        "total_factura_cents": _parse(tfac), "ret_garantia_pct": str(_parse(rpct)) if _parse(rpct) is not None else None,
                        "ret_garantia_base_cents": _parse(rbase), "ret_garantia_cents": _parse(rimp),
                        "irpf_pct": str(_parse(ipct)) if _parse(ipct) is not None else None, "irpf_cents": _parse(iimp),
                        "total_a_pagar_cents": _parse(tpag), "inversion_sujeto_pasivo": int(isp),
                        "exencion_motivo": exen or None, "iban": iban, "forma_pago": fpago or None, "notas": notas,
                        "cuenta_contable": cuenta}
                    for k in ("ret_garantia_cents", "irpf_cents"):
                        if cambios[k] is not None:
                            cambios[k] = abs(cambios[k])
                    if doc["estado"] == "sin_procesar":
                        cambios["estado"] = "pendiente_revision"
                    diff = ing.guardar_cabecera(con, doc_id, cambios, usuario())
                    _refrescar_huella()
                    st.toast(f"{len(diff)} campo(s) modificados" if diff else "Sin cambios")
                    st.rerun()
            for etiqueta, (okv, msg) in (("NIF emisor", validate_nif(doc["emisor_nif"])),
                                         ("IBAN", validate_iban(doc["iban"]) if doc["iban"] else (True, "sin IBAN"))):
                st.caption(f"{'' if okv else ''} {etiqueta}: {msg}")

        # ------------------------------------------------ líneas
        with t_lin:
            partidas = maestros.partidas_de_obra(con, doc["obra_id"]) if doc["obra_id"] else []
            if not partidas:
                st.info("Asigna una obra en la cabecera para poder imputar partidas.")
            lab = {p["id"]: f"{p['codigo']} · {p['descripcion']}" for p in partidas}
            inv = {v: k for k, v in lab.items()}
            df = pd.DataFrame([{
                "descripcion": l["descripcion"], "cantidad": _es(l["cantidad"]), "unidad": l["unidad"],
                "precio_unitario": _es(l["precio_unitario"]), "descuento_pct": _es(l["descuento_pct"]),
                "importe": _e_cents(l["importe_cents"]), "partida": lab.get(l["partida_id"]),
                "origen": l["partida_origen"], "tipo_linea": l["tipo_linea"] or "normal", "es_extra": bool(l["es_extra"]),
                "albaran": l["albaran"]} for l in doc["lineas"]],
                columns=["descripcion", "cantidad", "unidad", "precio_unitario", "descuento_pct", "importe", "partida",
                         "origen", "tipo_linea", "es_extra", "albaran"])
            for c_ in ("descripcion", "unidad", "cantidad", "precio_unitario", "descuento_pct", "importe", "origen", "albaran"):
                df[c_] = df[c_].fillna("").astype(str).replace("None", "")
            df["origen"] = df["origen"].map({"ia": "IA", "reglas": "Reglas", "manual": "Manual", "proveedor": "Habitual proveedor",
                                             "contexto": "Contexto factura", "cert": "Certificación"}).fillna(df["origen"])
            if partidas:
                cc1, cc2 = st.columns([3, 1])
                todas = cc1.selectbox("Asignar la misma partida a todas las líneas", [None] + list(lab.values()),
                                      key=f"todas_{doc_id}", format_func=lambda x: "— elegir partida —" if x is None else x)
                if cc2.button("Aplicar", key=f"apl_{doc_id}", disabled=not todas):
                    df["partida"] = todas
                    df["origen"] = "manual"
                    ing.guardar_lineas(con, doc_id, _df_a_lineas(df, inv), usuario()); st.rerun()
            ed = st.data_editor(df, num_rows="dynamic", width="stretch", key=f"lin_{doc_id}", column_config={
                "descripcion": st.column_config.TextColumn("Descripción", width="large"),
                "cantidad": "Cant.", "precio_unitario": "Precio", "descuento_pct": "% Dto",
                "unidad": st.column_config.SelectboxColumn("Ud.", options=["", "ud", "m", "m2", "m3", "kg", "t", "l", "h", "pa",
                                                                          "saco", "rollo", "caja", "palet", "jornada"]),
                "importe": st.column_config.TextColumn("Importe (€)", help="Formato 1234,56 · negativo para descuentos/anticipos"),
                "partida": st.column_config.SelectboxColumn("Partida", options=list(lab.values()), width="medium"),
                "origen": st.column_config.TextColumn("Origen", disabled=True, help="ia / reglas / manual"),
                "tipo_linea": st.column_config.SelectboxColumn("Tipo", options=TIPOS_LINEA),
                "es_extra": st.column_config.CheckboxColumn("Extra"), "albaran": st.column_config.TextColumn("Albarán (fecha)")})
            suma = sum((parse_amount(x) for x in ed["importe"].fillna("") if str(x).strip()), D(0))
            base = from_cents(doc["base_imponible_cents"] or 0)
            dif = suma - base
            st.markdown(f"Σ líneas **{fmt_eur(suma)}** · Base imponible **{fmt_eur(base)}** · "
                        f"Diferencia **{fmt_eur(dif, sign=True)}** {'' if abs(dif) <= D('0.02') else ''}")
            if st.button("Guardar líneas y revalidar", type="primary", key=f"gl_{doc_id}") and _sin_conflicto():
                malos = [str(r) for r in ed["importe"].fillna("") if str(r).strip() and not re.fullmatch(r"-?[\d.,\s]+€?", str(r).strip())]
                if malos:
                    st.error(f"Hay importes de línea que no son números: {', '.join(malos[:5])}. No se ha guardado nada.")
                    st.stop()
                ing.guardar_lineas(con, doc_id, _df_a_lineas(ed, inv), usuario())
                st.toast("Líneas guardadas"); st.rerun()

        # ------------------------------------------------ IVA
        with t_iva:
            dfi = pd.DataFrame([{"tipo_pct": _es(t["tipo_pct"]), "base": _e_cents(t["base_cents"]), "cuota": _e_cents(t["cuota_cents"]),
                                 "recargo_pct": _es(t["recargo_pct"]), "recargo": _e_cents(t["recargo_cents"])}
                                for t in doc["impuestos"]], columns=["tipo_pct", "base", "cuota", "recargo_pct", "recargo"])
            edi = st.data_editor(dfi, num_rows="dynamic", width="stretch", key=f"iva_{doc_id}", column_config={
                "tipo_pct": "% IVA", "base": "Base (€)", "cuota": "Cuota (€)", "recargo_pct": "% Rec. eq.", "recargo": "Recargo (€)"})
            if st.button("Guardar IVA y revalidar", key=f"gi_{doc_id}") and _sin_conflicto():
                imp = [{"tipo_pct": _parse(r.tipo_pct) or 0, "base": _parse(r.base), "cuota": _parse(r.cuota) or 0,
                        "recargo_pct": _parse(r.recargo_pct), "recargo": _parse(r.recargo) or 0}
                       for r in edi.fillna("").itertuples()]
                ing.guardar_impuestos(con, doc_id, imp, usuario()); st.rerun()

        # ------------------------------------------------ historial
        with t_hist:
            from core import historial as H
            vers = H.versiones(con, doc_id)
            st.markdown("**Versiones guardadas** (antes de cada cambio o relectura)")
            if not vers:
                st.caption("Aún no hay versiones anteriores.")
            else:
                import json as _json
                filas_v = []
                for v in vers:
                    f_ = _json.loads(v["foto"])
                    dd = f_["documento"]
                    filas_v.append({"id": v["id"], "Fecha": v["ts"].replace("T", " "), "Usuario": v["usuario"], "Motivo": v["motivo"],
                                    "Estado": dd.get("estado"), "Nº": dd.get("numero"), "Proveedor": dd.get("emisor_nombre"),
                                    "Base (€)": (dd.get("base_imponible_cents") or 0) / 100 if dd.get("base_imponible_cents") is not None else None,
                                    "Líneas": len(f_["lineas"])})
                st.dataframe(pd.DataFrame(filas_v).drop(columns=["id"]), hide_index=True, width="stretch")
                vsel = st.selectbox("Volver a la versión", [x["id"] for x in filas_v], key=f"vsel_{doc_id}",
                                    format_func=lambda i: next(f"{x['Fecha']} · {x['Motivo']}" for x in filas_v if x["id"] == i))
                if st.button("Restaurar esta versión", key=f"vrest_{doc_id}", icon=":material/history:",
                             help="La versión actual se guarda antes, así que también se puede volver a ella."):
                    H.restaurar_version(con, vsel, usuario()); st.toast("Versión restaurada"); st.rerun()
            st.markdown("**Registro de cambios**")
            h = db.rows(con, "SELECT ts, usuario, accion, detalle FROM auditoria WHERE entidad='documento' AND entidad_id=? "
                             "ORDER BY id DESC", (doc_id,))
            st.dataframe(pd.DataFrame(h), hide_index=True, width="stretch")
            if doc.get("extraccion_json"):
                with st.expander("Lectura original de la IA (JSON)"):
                    st.json(json.loads(doc["extraccion_json"]))

        # ------------------------------------------------ circuito
        st.divider()
        u1, u2, _ = st.columns([2, 2, 4])
        hay_cambio = db.one(con, "SELECT 1 x FROM auditoria WHERE entidad='documento' AND entidad_id=? AND accion='editar_cabecera'",
                            (doc_id,))
        if u1.button("Deshacer último cambio de cabecera", icon=":material/undo:", disabled=not hay_cambio):
            diff = ing.deshacer_cabecera(con, doc_id, usuario())
            st.toast(f"Deshecho: {', '.join(diff) if diff else 'nada'}"); st.rerun()
        if doc["estado"] != "eliminado":
            if u2.button("Enviar a la papelera", icon=":material/delete:", help="Se puede restaurar desde Documentos y pagos → Papelera."):
                ing.a_papelera(con, doc_id, usuario()); st.toast("Enviado a la papelera"); st.rerun()
        else:
            if u2.button("Restaurar de la papelera", icon=":material/restore_from_trash:"):
                ing.restaurar(con, doc_id, usuario()); st.rerun()
        faltan = ing.requisitos_aprobacion(con, doc_id, rol())
        if doc.get("conformado_por"):
            st.success(f"Conformidad de obra: {doc['conformado_por']} ({str(doc['conformado_en']).replace('T', ' ')})")
        cf1, cf2 = st.columns([2, 3])
        if puede_conformar() and not doc.get("conformado_por") and doc["estado"] not in ("rechazada", "eliminado", "duplicado"):
            if cf1.button("Dar conformidad de obra", icon=":material/verified:",
                          help="El jefe de obra confirma que el trabajo o material se ha recibido y que la obra y partidas son correctas."):
                ok, msg = ing.conformar(con, doc_id, usuario(), rol(), obras_permitidas())
                (st.toast if ok else st.error)(msg)
                if ok:
                    st.rerun()
        if faltan and doc["estado"] != "aprobada":
            cf2.caption("Para aprobar: " + " ".join(faltan))
        b1, b2, b3, b4, b5 = st.columns(5)
        if b1.button("Marcar revisada", disabled=doc["estado"] in ("revisada", "aprobada")):
            ing.cambiar_estado(con, doc_id, "revisada", usuario()); st.rerun()
        if b2.button("Aprobar", type="primary", disabled=doc["estado"] == "aprobada" or not puede_aprobar()):
            ok, msg = ing.cambiar_estado(con, doc_id, "aprobada", usuario(), rol=rol())
            if ok:
                st.toast("Aprobada "); st.rerun()
            else:
                st.error(msg)
        if b3.button("Rechazar", disabled=doc["estado"] == "rechazada"):
            ing.cambiar_estado(con, doc_id, "rechazada", usuario()); st.rerun()
        if b4.button("Volver a pendiente", disabled=doc["estado"] in ("pendiente_revision", "sin_procesar")):
            ing.cambiar_estado(con, doc_id, "pendiente_revision", usuario()); st.rerun()
        if b5.button("Volver a leer", disabled=lector.necesita_api(con) and not api_key(),
                     help="Sustituye los datos actuales por una nueva lectura con el motor configurado."):
            with st.spinner("Leyendo…"):
                try:
                    obras = maestros.listar_obras(con)
                    part = maestros.partidas_de_obra(con, doc["obra_id"] or obras[0]["id"]) if obras else []
                    if not doc["file_path"]:
                        raise ExtractionError("Documento sin PDF.")
                    res = lector.leer(con, Path(doc["file_path"]).read_bytes(), obras, part, api_key(), modelo(), filename=doc["filename"])
                    ing.aplicar_extraccion(con, doc_id, res, usuario()); st.rerun()
                except ExtractionError as e:
                    st.error(str(e))


def _df_a_lineas(df: pd.DataFrame, inv: dict) -> list[dict]:
    out = []
    for r in df.fillna("").to_dict("records"):
        if not str(r.get("descripcion") or "").strip() and not str(r.get("importe") or "").strip():
            continue
        out.append({
            "descripcion": r.get("descripcion") or None, "cantidad": _parse(str(r.get("cantidad") or "")),
            "unidad": r.get("unidad") or None, "precio_unitario": _parse(str(r.get("precio_unitario") or "")),
            "descuento_pct": _parse(str(r.get("descuento_pct") or "")), "importe": _parse(str(r.get("importe") or "")) or 0,
            "partida_id": inv.get(r.get("partida")), "partida_origen": r.get("origen") or "manual",
            "tipo_linea": r.get("tipo_linea") or "normal", "es_extra": bool(r.get("es_extra")),
            "albaran": r.get("albaran") or None})
    return out
