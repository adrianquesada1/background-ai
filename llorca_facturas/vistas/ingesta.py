"""Entrada de facturas: subida de PDFs/ZIP y lectura con IA en lote."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import streamlit as st

from core import db, maestros, ingesta as ing, trabajos
from core.config import BASE_DIR
from core.extractor import ExtractionError
from core import lector
from core.pdf_utils import iter_pdfs_from_upload
from vistas.comun import get_con, usuario, api_key, modelo, badge_estado, selector_obra
from core.money import parse_amount

DEMO_DIR = BASE_DIR / "demo"


def _leer_lote(con, ids: list[int], hilos: int, obra_lote: int | None = None):
    key, mod = api_key(), modelo()
    obras = maestros.listar_obras(con, solo_activas=True)
    ref = obra_lote or (obras[0]["id"] if obras else None)
    partidas = maestros.partidas_de_obra(con, ref) if ref else []
    docs = {r["id"]: r for r in db.rows(con, f"SELECT id, filename, file_path FROM documentos WHERE id IN ({','.join('?'*len(ids))})", ids)}
    cfg = lector.config(con)
    provs = {r["nif"]: r["nombre"] for r in db.rows(con, "SELECT nif, nombre FROM proveedores WHERE nif IS NOT NULL")}
    barra = st.progress(0.0, text="Preparando…")
    tabla = st.empty()
    filas, errores = [], []
    t_ini = time.time()

    def registrar(did, res=None, err=None):
        if res is not None:
            try:
                ing.aplicar_extraccion(con, did, res, usuario(), obra_defecto=obra_lote)
                d = db.one(con, "SELECT emisor_nombre, numero, base_imponible_cents, estado, confianza FROM documentos WHERE id=?", (did,))
                inc = db.one(con, "SELECT COUNT(*) n FROM incidencias WHERE documento_id=? AND resuelta=0 AND severidad IN ('critica','alta')",
                             (did,))["n"]
                filas.append({"Documento": docs[did]["filename"], "Proveedor": d["emisor_nombre"], "Nº": d["numero"],
                              "Base (€)": (d["base_imponible_cents"] or 0) / 100, "Confianza": d["confianza"],
                              "Resultado": "Duplicado" if d["estado"] == "duplicado" else ("Revisar" if inc else "Correcto"),
                              "Lectura": res.get("modelo"), "Segundos": res.get("segundos")})
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
        if err:
            errores.append((docs[did]["filename"], err))
            filas.append({"Documento": docs[did]["filename"], "Resultado": "Error", "Lectura": err[:120]})
        n = len(filas)
        restante = (time.time() - t_ini) / n * (len(ids) - n)
        barra.progress(n / len(ids), text=f"{n} de {len(ids)} · quedan unos {int(restante // 60)} min {int(restante % 60)} s")
        tabla.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch",
                        column_config={"Base (€)": st.column_config.NumberColumn(format="localized"),
                                       "Confianza": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})

    if cfg["motor"] == "claude":
        # en la nube: lecturas en paralelo
        def tarea(did):
            return did, lector.leer(con, Path(docs[did]["file_path"]).read_bytes(), obras, partidas, key, mod, cfg, provs,
                                    docs[did]["filename"])
        with ThreadPoolExecutor(max_workers=hilos) as ex:
            futs = {ex.submit(tarea, did): did for did in ids}
            for fut in as_completed(futs):
                did = futs[fut]
                try:
                    registrar(did, fut.result()[1])
                except Exception as e:  # noqa: BLE001
                    registrar(did, err=str(e))
    else:
        # en local: de una en una (la IA local usa toda la CPU; en paralelo solo se estorban)
        for did in ids:
            barra.progress(len(filas) / len(ids), text=f"Leyendo {docs[did]['filename']} ({len(filas) + 1} de {len(ids)})…")
            try:
                res = lector.leer(con, Path(docs[did]["file_path"]).read_bytes(), obras, partidas, key, mod, cfg, provs,
                                  docs[did]["filename"])
                registrar(did, res)
            except Exception as e:  # noqa: BLE001
                registrar(did, err=f"{type(e).__name__}: {e}")
            provs = {r["nif"]: r["nombre"] for r in db.rows(con, "SELECT nif, nombre FROM proveedores WHERE nif IS NOT NULL")}
    barra.progress(1.0, text=f"Terminado en {int(time.time() - t_ini)} s")
    ok = sum(1 for f in filas if f.get("Resultado") in ("Correcto", "Revisar"))
    return ok, errores, filas


def render():
    con = get_con()
    st.title("Entrada de facturas")
    st.caption("Sube PDFs sueltos o un ZIP. Cada PDF se identifica por su huella SHA-256: "
               "el mismo archivo nunca se registra dos veces.")

    from core.pdf_utils import EXT_ADMITIDAS
    c1, c2 = st.columns([3, 2])
    with c1:
        obra_sub = selector_obra("ing_obra_subida", label="Obra de estos documentos (opcional)",
                                 texto_ninguna="— detectar automáticamente en cada factura —")
    forzar = c2.checkbox("Forzar esta obra aunque el documento indique otra", key="ing_forzar", disabled=obra_sub is None,
                         help="Sin marcar: se usa la obra que indique cada factura y, si no indica ninguna, la elegida aquí. "
                              "Si una factura dice otra obra distinta, queda una incidencia para revisarlo.")
    files = st.file_uploader("Facturas: PDF, fotos o escaneos (JPG, PNG, TIFF), ZIP o correos (.eml/.msg) con adjuntos",
                             type=EXT_ADMITIDAS, accept_multiple_files=True)

    def _registrar(origenes):
        from core import historial as _H
        _H.init(con)
        lote = _H.nuevo_lote()
        res = {"nuevos": [], "repetidos": [], "duplicados": [], "rechazados": []}
        for nombre_origen, contenido in origenes:
            try:
                partes = list(iter_pdfs_from_upload(nombre_origen, contenido))
            except Exception as e:  # noqa: BLE001
                res["rechazados"].append((nombre_origen, f"no se puede abrir ({e})"))
                continue
            if not partes:
                res["rechazados"].append((nombre_origen, "no contiene PDF ni imágenes de documento"))
            for nombre, data in partes:
                motivo = ing.comprobar_archivo(nombre, data)
                if motivo:
                    res["rechazados"].append((nombre, motivo))
                    continue
                did, es_nuevo = ing.registrar_pdf(con, nombre, data, usuario(), obra_sub, forzar, lote)
                if not es_nuevo:
                    ex = db.one(con, "SELECT filename, estado FROM documentos WHERE id=?", (did,))
                    res["repetidos"].append((nombre, f"ya estaba registrado como #{did} {ex['filename']}"
                                                     + (" (en la papelera)" if ex["estado"] == "eliminado" else "")))
                elif db.one(con, "SELECT estado FROM documentos WHERE id=?", (did,))["estado"] == "duplicado":
                    res["duplicados"].append((nombre, db.one(con, "SELECT notas FROM documentos WHERE id=?", (did,))["notas"]))
                else:
                    res["nuevos"].append(nombre)
        st.success(f"{len(res['nuevos'])} documento(s) nuevos registrados.")
        if res["nuevos"]:
            st.session_state["_ultimo_lote"] = (lote, len(res["nuevos"]))
        if res["repetidos"] or res["duplicados"]:
            st.warning(f"{len(res['repetidos']) + len(res['duplicados'])} documento(s) ya existían y NO se han añadido:\n\n" +
                       "\n".join(f"- {n}: {m}" for n, m in res["repetidos"] + res["duplicados"]))
        if res["rechazados"]:
            st.error(f"{len(res['rechazados'])} archivo(s) rechazados:\n\n" + "\n".join(f"- {n}: {m}" for n, m in res["rechazados"]))

    if st.session_state.get("_ultimo_lote"):
        l_, n_ = st.session_state["_ultimo_lote"]
        cc1, cc2 = st.columns([4, 1])
        cc1.caption(f"Última subida: {n_} documento(s). Si se ha equivocado de archivos o de obra, puede deshacerla entera.")
        if cc2.button("Deshacer esta subida", icon=":material/undo:"):
            from core import historial as _H
            k = _H.deshacer_subida(con, l_, usuario())
            st.session_state.pop("_ultimo_lote", None)
            st.toast(f"{k} documento(s) enviados a la papelera (restaurables)"); st.rerun()
    if files and st.button("Registrar documentos", type="primary"):
        with st.spinner("Comprobando y registrando…"):
            _registrar([(f.name, f.getvalue()) for f in files])

    with st.expander("…o importar desde una carpeta del equipo"):
        carpeta = st.text_input("Ruta de la carpeta", placeholder=r"C:\Users\pc\Downloads\AlibuildingFacturas")
        if carpeta and st.button("Importar carpeta"):
            p = Path(carpeta.strip().strip('"'))
            if not p.is_dir():
                st.error("La carpeta no existe o no es accesible desde el servidor.")
            else:
                from core.pdf_utils import EXT_ADMITIDAS as _EXT
                with st.spinner("Comprobando y registrando…"):
                    _registrar([(fx.name, fx.read_bytes()) for fx in sorted(p.rglob("*"))
                                if fx.is_file() and fx.suffix.lower().lstrip(".") in _EXT])

    st.subheader("Lectura de facturas")
    st.caption("La lectura se ejecuta en segundo plano: puede cambiar de pantalla o cerrar el navegador y continuará. "
               "Si se reinicia la aplicación, sigue donde se quedó.")
    en_cola = trabajos.en_cola(con)
    pend = pd.DataFrame(db.rows(con, "SELECT id, filename, paginas, tiene_texto, estado, creado_en FROM documentos "
                                     "ORDER BY (estado='sin_procesar') DESC, id DESC"))
    if pend.empty:
        st.info("Aún no hay documentos.")
    else:
        sin = pend[(pend["estado"] == "sin_procesar") & (~pend["id"].isin(en_cola))]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Sin leer", len(sin))
        c2.metric("En cola", len(en_cola))
        c3.metric("Escaneados (sin texto)", int((pend["tiene_texto"] == 0).sum()),
                  help="Se leen con OCR y, si hay un modelo Vision, mirando la imagen. Revisión obligatoria.")
        c4.metric("Duplicados apartados", int((pend["estado"] == "duplicado").sum()))

        modo = st.radio("¿Qué leer?", ["Todos los pendientes", "Seleccionar"], horizontal=True, key="ing_modo")
        if modo == "Seleccionar":
            ids = st.multiselect("Documentos", pend["id"].tolist(), key="ing_sel",
                                 format_func=lambda i: f"#{i} · {pend.set_index('id').loc[i, 'filename']} "
                                                       f"({pend.set_index('id').loc[i, 'estado']})")
        else:
            ids = sin["id"].tolist()
        ids = [i for i in ids if i not in en_cola]
        obra_lote = selector_obra("ing_obra_lote", label="Obra de este lote (recomendado si todas son de la misma obra)",
                                  texto_ninguna="— detectar en cada factura —")
        releer = [i for i in ids if pend.set_index("id").loc[i, "estado"] not in ("sin_procesar",)]
        if releer:
            st.warning(f"{len(releer)} documento(s) ya leídos se volverán a leer. Sus datos actuales (incluidas las correcciones "
                       "manuales) se guardan como versión y se pueden recuperar desde Revisión → Historial.")
        cfg = lector.config(con)
        st.caption(f"Motor de lectura: **{lector.MOTORES[cfg['motor']]}** (se cambia en Configuración)")
        modo_ia = "auto"
        if cfg["motor"] == "local_auto":
            modo_ia = st.radio("IA local en este lote", ["auto", "nunca", "siempre"], horizontal=True, key="ing_modo_ia",
                               format_func={"auto": "Solo cuando falten datos (recomendado)",
                                            "nunca": "Nunca: máxima velocidad (reglas + OCR)",
                                            "siempre": "Siempre: doble contraste (lento en CPU)"}.get)
        disabled = (cfg["motor"] == "claude" and not api_key()) or not ids
        if st.button(f"Leer {len(ids)} documento(s) en segundo plano", type="primary", disabled=disabled,
                     icon=":material/play_arrow:"):
            if cfg["motor"] == "claude" and api_key():
                db.set_setting(con, "anthropic_api_key", api_key())   # el proceso de fondo no ve la sesión del navegador
            tid = trabajos.crear_lectura(con, ids, obra_lote, usuario(), modo_ia)
            st.toast(f"Lectura nº {tid} encolada: {len(ids)} documento(s)")
            st.rerun()

    en_cola_ahora = trabajos.en_cola(con)
    if en_cola_ahora:
        with st.popover(f"Vaciar la cola ({len(en_cola_ahora)} documento(s) en espera)", icon=":material/playlist_remove:"):
            st.caption("Se quitan de la cola todos los documentos que aún no han empezado a leerse. El que se esté leyendo en este "
                       "momento termina. Los documentos no se borran: quedan «sin leer» para lanzarlos cuando quiera.")
            if st.button("Confirmar: vaciar la cola", type="primary", key="vaciar_cola"):
                n_l, n_d = trabajos.vaciar_cola(con, usuario())
                st.toast(f"Cola vaciada: {n_d} documento(s) quitados de {n_l} lectura(s)")
                st.rerun()
    _panel_trabajos(con)

    if not pend.empty:
        with st.expander("Todos los documentos registrados"):
            st.dataframe(pend, hide_index=True, width="stretch", column_config={
                "tiene_texto": st.column_config.CheckboxColumn("Capa de texto"), "paginas": "Págs.",
                "filename": "Archivo", "creado_en": "Alta"})

    with st.expander("Registrar una factura a mano (sin PDF o ilegible)"):
        _alta_manual(con)

    st.subheader("Datos de demostración")
    st.caption("9 facturas reales de la obra 664 transcritas y verificadas a mano + la certificación nº 21. Permiten probar "
               "rentabilidad, revisión y asistente sin gastar API.")
    if st.button("Cargar demo verificada", disabled=not (BASE_DIR / "tests" / "casos_reales.py").exists(),
                 help="Requiere los PDF reales en demo/ y tests/casos_reales.py (no se publican en el repositorio)."):
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from tests.casos_reales import CASOS
        n = 0
        for fn, data in CASOS.items():
            p = DEMO_DIR / fn
            if not p.exists():
                continue
            did, nuevo = ing.registrar_pdf(con, fn, p.read_bytes(), usuario())
            estado = db.one(con, "SELECT estado FROM documentos WHERE id=?", (did,))["estado"]
            if estado == "sin_procesar":
                ing.aplicar_extraccion(con, did, {"data": data, "modelo": "transcripción manual verificada"}, usuario())
                n += 1
        cert_pdf = DEMO_DIR / "Certificacion_21.pdf"
        if cert_pdf.exists():
            from core import certificacion as C, obra_control as oc
            obra = db.one(con, "SELECT id FROM obras WHERE codigo='664'")
            if obra and not db.one(con, "SELECT id FROM certificaciones WHERE obra_id=? AND numero=21", (obra["id"],)):
                with st.spinner("Importando la certificación nº 21 y adoptando su estructura de capítulos…"):
                    c = C.leer_pdf(cert_pdf.read_bytes())
                    cid = oc.guardar_certificacion(con, c, cert_pdf.read_bytes(), cert_pdf.name, obra["id"], usuario())
                    oc.adoptar_estructura(con, obra["id"], cid, usuario())
                from core import auditoria as A
                A.asegurar_oficios(con, obra["id"])
                A.clasificar_partidas_auto(con, obra["id"], cid)
                A.clasificar_proveedores_auto(con, obra["id"])
        st.success(f"{n} documentos de demostración cargados, más la certificación nº 21 de la obra 664, con los oficios propuestos automáticamente.")


def _alta_manual(con):
    obras = maestros.listar_obras(con, solo_activas=True)
    if not obras:
        st.info("Crea antes una obra.")
        return
    with st.form("alta_manual", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 1, 1])
        obra_id = c1.selectbox("Obra", [o["id"] for o in obras],
                               format_func=lambda i: next(f"{o['codigo']} · {o['nombre']}" for o in obras if o["id"] == i))
        tipo = c2.selectbox("Tipo", ["factura", "abono", "anticipo"])
        fecha = c3.date_input("Fecha", format="DD/MM/YYYY")
        c4, c5, c6 = st.columns([2, 1, 1])
        emisor = c4.text_input("Proveedor *")
        nif = c5.text_input("NIF *")
        numero = c6.text_input("Nº factura *")
        partidas = maestros.partidas_de_obra(con, obra_id)
        c7, c8 = st.columns([2, 2])
        partida = c7.selectbox("Capítulo / partida", [p["codigo"] for p in partidas],
                               format_func=lambda c: next(f"{p['codigo']} · {p['descripcion']}" for p in partidas if p["codigo"] == c))
        concepto = c8.text_input("Concepto")
        c9, c10, c11, c12 = st.columns(4)
        base = c9.text_input("Base imponible (€) *", placeholder="15000,00")
        iva = c10.selectbox("IVA %", ["21", "10", "4", "0"])
        ret = c11.selectbox("Ret. garantía %", ["0", "5", "10"])
        irpf = c12.selectbox("IRPF %", ["0", "7", "15", "19"])
        isp = st.checkbox("Inversión del sujeto pasivo (subcontrata de obra, IVA 0)")
        iban = st.text_input("IBAN (opcional)")
        if st.form_submit_button("Registrar", type="primary"):
            if not (emisor and nif and numero and base):
                st.error("Faltan campos obligatorios (*).")
            else:
                did = ing.alta_manual(con, {"obra_id": obra_id, "tipo": tipo, "fecha": fecha.isoformat(), "emisor_nombre": emisor,
                                            "emisor_nif": nif, "numero": numero, "partida_codigo": partida, "concepto": concepto,
                                            "base": str(parse_amount(base)) if tipo != "abono" else str(-abs(parse_amount(base))),
                                            "tipo_iva": "0" if isp else iva, "ret_pct": ret, "irpf_pct": irpf, "isp": isp,
                                            "iban": iban}, usuario())
                st.success(f"Registrada como documento #{did}. Revísala y apruébala en  Revisión.")


@st.fragment(run_every=3)
def _panel_trabajos(con):
    """Progreso de las lecturas en segundo plano (se actualiza solo cada pocos segundos)."""
    import json as _json
    recientes = trabajos.recientes(con, 5)
    if not recientes:
        return
    st.markdown("**Lecturas**")
    ETQ = {"pendiente": "En espera", "en_curso": "En curso", "terminado": "Terminada", "cancelado": "Cancelada"}
    t = recientes[0]
    est = trabajos.estadisticas(con, t)

    def _dur(seg):
        if seg is None:
            return "—"
        if seg < 10:
            return f"{seg:.1f} s".replace(".", ",")
        seg = int(seg)
        return f"{seg // 3600} h {seg % 3600 // 60} min" if seg >= 3600 else (f"{seg // 60} min {seg % 60} s" if seg >= 60 else f"{seg} s")
    with st.container(border=True):
        c1, c2 = st.columns([5, 1])
        c1.markdown(f"**Lectura nº {t['id']}** · {ETQ.get(t['estado'], t['estado'])} · lanzada por {t['creado_por']} el "
                    f"{t['creado_en'].replace('T', ' ')}")
        if t["estado"] in ("pendiente", "en_curso") and c2.button("Cancelar", key=f"cancel_{t['id']}"):
            trabajos.cancelar(con, t["id"], usuario())
        k = st.columns(4)
        k[0].metric("Progreso", f"{est['pct']} %", f"{t['hechos']} de {t['total']}", delta_color="off")
        k[1].metric("Tiempo medio por documento", _dur(est["media"]))
        k[2].metric("Tiempo restante estimado", _dur(est["eta"]) if t["estado"] in ("pendiente", "en_curso") else "—")
        k[3].metric("Errores", t["errores"] or 0)
        st.progress(est["pct"] / 100)
        if est["actual"]:
            st.caption(f"Leyendo ahora: **{est['actual']}** · {_dur(est['lleva'])}. Las facturas que cuadran por reglas tardan "
                       "menos de un segundo; si hace falta la IA local, en CPU puede tardar uno o dos minutos.")
        elif t["estado"] == "pendiente":
            st.caption("En espera: empezará en unos segundos.")
        filas = []
        for it in trabajos.items(con, t["id"]):
            r = _json.loads(it["resultado"]) if it["resultado"] else {}
            leyendo = it["estado"] == "pendiente" and it.get("iniciado_en")
            filas.append({"Documento": it["filename"],
                          "Estado": "Leyendo…" if leyendo else {"ok": "Correcto", "revisar": "Revisar", "duplicado": "Duplicado",
                                                                "error": "Error", "pendiente": "En espera",
                                                                "cancelado": "Cancelado"}.get(it["estado"], it["estado"]),
                          "Proveedor": r.get("proveedor") or "", "Nº": r.get("numero") or "",
                          "Base (€)": (r.get("base_cents") or 0) / 100 if r.get("base_cents") is not None else None,
                          "Confianza": r.get("confianza"), "Segundos": it["segundos"],
                          "Detalle": it["error"] or it["lectura"] or ""})
        if filas:
            st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch", height=min(460, 38 + 35 * len(filas)),
                         column_config={"Base (€)": st.column_config.NumberColumn(format="localized"),
                                        "Segundos": st.column_config.NumberColumn(format="%.1f"),
                                        "Confianza": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
    if len(recientes) > 1:
        st.caption("Anteriores: " + " · ".join(f"nº {x['id']} {ETQ.get(x['estado'], x['estado']).lower()} "
                                                 f"({x['hechos']}/{x['total']})" for x in recientes[1:]))
