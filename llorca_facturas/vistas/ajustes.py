"""Configuración (API, modelo, empresa), auditoría completa y copia de seguridad."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from core import db, lector
from core.extractor_local import ollama_disponible
from core.config import MODELOS_CLAUDE, EMPRESA_NIF, DB_PATH
from core.fiscal import validate_nif
from vistas.comun import get_con, api_key, modelo, usuario, rol


def _usuarios(con):
    from core import usuarios as U
    st.subheader("Usuarios")
    us = pd.DataFrame(U.listar(con))
    if not us.empty:
        us["rol"] = us["rol"].map(U.ROLES)
        us["activo"] = us["activo"].astype(bool)
        st.dataframe(us.drop(columns=["id"]), hide_index=True, width="stretch",
                     column_config={"usuario": "Usuario", "nombre": "Nombre", "rol": "Rol", "activo": "Activo",
                                    "ultimo_acceso": "Último acceso", "creado_en": "Alta"})
    c1, c2 = st.columns(2)
    with c1.form("nuevo_usuario", clear_on_submit=True):
        st.markdown("**Nuevo usuario**")
        u = st.text_input("Usuario", placeholder="nombre.apellido")
        n = st.text_input("Nombre completo")
        r = st.selectbox("Cargo / rol", list(U.ROLES), format_func=U.ROLES.get, index=2,
                         help="\n".join(f"{U.ROLES[k]}: {v}" for k, v in U.DESCRIPCION_ROLES.items()))
        c = st.text_input("Contraseña inicial", type="password")
        if st.form_submit_button("Crear usuario", type="primary"):
            try:
                U.crear(con, u, n or u, c, r, usuario())
                st.success("Usuario creado."); st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    with c2.form("editar_usuario"):
        st.markdown("**Modificar usuario**")
        lista = U.listar(con)
        if not lista:
            st.caption("Aún no hay usuarios.")
            st.form_submit_button("Guardar cambios", disabled=True)
            return
        sel = st.selectbox("Usuario", [x["id"] for x in lista], format_func=lambda i: next(x["usuario"] for x in lista if x["id"] == i))
        actual = next(x for x in lista if x["id"] == sel)
        r = st.selectbox("Rol ", list(U.ROLES), format_func=U.ROLES.get, index=list(U.ROLES).index(actual["rol"]))
        act = st.checkbox("Activo", bool(actual["activo"]))
        nueva = st.text_input("Nueva contraseña (opcional)", type="password")
        b1, b2 = st.columns(2)
        guardar = b1.form_submit_button("Guardar cambios", type="primary")
        borrar = b2.form_submit_button("Eliminar usuario")
        if guardar:
            try:
                U.actualizar(con, sel, r, act, usuario())
                if nueva:
                    U.cambiar_clave(con, sel, nueva, usuario())
                    from core import sesiones
                    sesiones.cerrar_de_usuario(con, sel)       # con clave nueva, se cierran sus sesiones abiertas
                if not act:
                    from core import sesiones
                    sesiones.cerrar_de_usuario(con, sel)
                st.success("Guardado."); st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
        if borrar:
            if sel == (st.session_state.get("auth") or {}).get("id"):
                st.error("No puede eliminarse a sí mismo.")
            else:
                try:
                    U.eliminar(con, sel, usuario())
                    st.success("Usuario eliminado."); st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))

    with st.expander("Qué puede hacer cada cargo"):
        st.dataframe(pd.DataFrame([{"Cargo": U.ROLES[k], "Permisos": v} for k, v in U.DESCRIPCION_ROLES.items()]),
                     hide_index=True, width="stretch")

    st.subheader("Circuito de aprobación")
    from core import ingesta as _ing
    rg = _ing.reglas_aprobacion(con)
    with st.form("reglas_aprob"):
        c1, c2, c3 = st.columns(3)
        conf = c1.checkbox("Exigir conformidad del jefe de obra antes de aprobar", rg["conformidad"],
                           help="Solo en obras que tengan un jefe de obra asignado.")
        umbral = c2.text_input("Importe a partir del cual aprueba Dirección (€)", f"{rg['umbral_cents'] / 100:.2f}".replace(".", ","))
        dias = c3.number_input("Aviso de factura retrasada (días sin aprobar)", 1, 180, rg["dias_aviso"])
        if st.form_submit_button("Guardar reglas"):
            from core.money import parse_amount, to_cents
            db.set_setting(con, "exigir_conformidad", "1" if conf else "0")
            db.set_setting(con, "umbral_direccion_cents", str(to_cents(parse_amount(umbral))))
            db.set_setting(con, "dias_aviso_aprobacion", str(int(dias)))
            db.audit(con, usuario(), "reglas_aprobacion", "ajustes", None, {"conformidad": conf, "umbral": umbral, "dias": dias}); con.commit()
            st.success("Reglas guardadas.")

    st.subheader("Tesorería y contabilidad")
    with st.form("aj_tes"):
        c1, c2, c3 = st.columns(3)
        series = c1.text_input("Series SII de facturas emitidas (separadas por comas)", db.get_setting(con, "series_sii", "F1,R1"))
        ivav = c2.text_input("IVA por defecto en ventas (%)", db.get_setting(con, "iva_venta_defecto", "10"))
        retc = c3.text_input("Retención de garantía del cliente por defecto (%)", db.get_setting(con, "retencion_cliente_defecto", "5"))
        c4, c5, c6 = st.columns(3)
        dv = c4.number_input("Días de vencimiento de las facturas emitidas", 0, 365, int(db.get_setting(con, "dias_vencimiento_venta", "60")))
        dr = c5.number_input("Días hasta la devolución de la retención", 30, 1825, int(db.get_setting(con, "dias_retencion_cliente", "365")))
        da = c6.number_input("Aviso previo de retención (días)", 0, 180, int(db.get_setting(con, "aviso_retencion_dias", "30")))
        st.markdown("**Cuentas contables de los asientos**")
        k1, k2, k3, k4, k5 = st.columns(5)
        cp = k1.text_input("Proveedores", db.get_setting(con, "cta_proveedores", "400"))
        ci = k2.text_input("IVA soportado", db.get_setting(con, "cta_iva_soportado", "472"))
        cr = k3.text_input("IVA repercutido ISP", db.get_setting(con, "cta_iva_repercutido_isp", "477"))
        cf = k4.text_input("Retenciones IRPF", db.get_setting(con, "cta_irpf", "4751"))
        cg = k5.text_input("Retenciones de garantía", db.get_setting(con, "cta_ret_garantia_prov", "4009"))
        if st.form_submit_button("Guardar tesorería y contabilidad"):
            for k_, v_ in (("series_sii", series), ("iva_venta_defecto", ivav), ("retencion_cliente_defecto", retc),
                           ("dias_vencimiento_venta", str(dv)), ("dias_retencion_cliente", str(dr)), ("aviso_retencion_dias", str(da)),
                           ("cta_proveedores", cp), ("cta_iva_soportado", ci), ("cta_iva_repercutido_isp", cr), ("cta_irpf", cf),
                           ("cta_ret_garantia_prov", cg)):
                db.set_setting(con, k_, str(v_).strip())
            db.audit(con, usuario(), "ajustes_tesoreria", "ajustes", None, None); con.commit()
            st.success("Guardado.")

    st.subheader("Pagos SEPA (cuenta ordenante)")
    with st.form("aj_sepa"):
        c1, c2, c3, c4 = st.columns(4)
        sn = c1.text_input("Nombre del ordenante", db.get_setting(con, "sepa_nombre", "LLORCA GROUP HISPANIA SL"))
        si = c2.text_input("IBAN de la cuenta de pago", db.get_setting(con, "sepa_iban", ""))
        sb = c3.text_input("BIC (opcional)", db.get_setting(con, "sepa_bic", ""))
        sd = c4.text_input("Identificador / NIF (opcional)", db.get_setting(con, "sepa_id", ""))
        if st.form_submit_button("Guardar ordenante"):
            from core.fiscal import validate_iban, normalize_iban
            if si and not validate_iban(normalize_iban(si))[0]:
                st.error("El IBAN no es válido.")
            else:
                for k_, v_ in (("sepa_nombre", sn), ("sepa_iban", normalize_iban(si) if si else ""), ("sepa_bic", sb), ("sepa_id", sd)):
                    db.set_setting(con, k_, v_.strip())
                db.audit(con, usuario(), "ajustes_sepa", "ajustes", None, {"iban": si[-4:] if si else ""}); con.commit()
                st.success("Guardado.")

    st.subheader("Costes y ventas internos")
    oblig = st.checkbox("Exigir justificante adjunto para validar un movimiento interno",
                        db.get_setting(con, "internos_adjunto_obligatorio", "1") == "1", key="aj_int_adj")
    if oblig != (db.get_setting(con, "internos_adjunto_obligatorio", "1") == "1"):
        db.set_setting(con, "internos_adjunto_obligatorio", "1" if oblig else "0")
        db.audit(con, usuario(), "ajuste_internos", "ajustes", None, {"adjunto_obligatorio": oblig}); con.commit()

    st.subheader("Sociedades del grupo")
    import json as _json
    from core.validation import sociedades as _soc
    socs = _soc(con)
    ed = st.data_editor(pd.DataFrame([{"NIF": k, "Nombre": v} for k, v in socs.items()]), num_rows="dynamic", hide_index=True,
                        key="soc_ed", width="stretch")
    st.caption("Las facturas deben ir a nombre de una de estas sociedades; si el nombre no coincide con el NIF se avisa.")
    if st.button("Guardar sociedades"):
        from core.fiscal import validate_nif, normalize_nif
        nuevas = {normalize_nif(r["NIF"]): (r["Nombre"] or "").strip() for r in ed.fillna("").to_dict("records") if str(r["NIF"]).strip()}
        malos = [k for k in nuevas if not validate_nif(k)[0]]
        if malos or not nuevas:
            st.error("NIF no válido: " + ", ".join(malos) if malos else "Debe haber al menos una sociedad.")
        else:
            db.set_setting(con, "sociedades", _json.dumps(nuevas, ensure_ascii=False))
            db.audit(con, usuario(), "sociedades_grupo", "ajustes", None, nuevas); con.commit()
            st.success("Sociedades guardadas.")

    st.subheader("Sesiones abiertas")
    from core import sesiones
    ses = sesiones.activas(con)
    if ses:
        vis = pd.DataFrame(ses)
        vis["recordar"] = vis["recordar"].astype(bool)
        st.dataframe(vis.drop(columns=["huella", "id"]), hide_index=True, width="stretch",
                     column_config={"usuario": "Usuario", "nombre": "Nombre", "rol": "Rol", "creada": "Inicio", "ultima": "Última actividad",
                                    "expira": "Caduca", "recordar": "Recordada", "ultima_pagina": "Última página", "navegador": "Navegador", "ip": "IP"})
        cerrar = st.selectbox("Cerrar la sesión de", [x["huella"] for x in ses],
                              format_func=lambda h: next(f"{x['usuario']} · {x['ip'] or ''} · desde {x['creada']}" for x in ses if x["huella"] == h))
        if st.button("Cerrar esta sesión"):
            sesiones.revocar(con, cerrar)
            db.audit(con, usuario(), "revocar_sesion", "sesion", None, {"sesion": cerrar[:10]}); con.commit()
            st.rerun()

    st.subheader("Acceso desde otros equipos")
    import socket
    try:
        ip_lan = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip_lan = "IP-del-servidor"
    st.markdown(f"Con la aplicación arrancada con **servidor.bat**, cualquier equipo de la red de la oficina accede en "
                f"**http://{ip_lan}:8501** (o http://{socket.gethostname()}:8501). Todos trabajan sobre la misma base de datos "
                "y ven la misma información en tiempo real.")


def render():
    con = get_con()
    st.title("Configuración y auditoría")
    if rol() != "admin":
        st.warning("Solo los administradores pueden cambiar la configuración.")
        return
    _usuarios(con)

    st.subheader("Motor de lectura de facturas")
    cfg = lector.config(con)
    motor = st.radio("Motor", list(lector.MOTORES), index=list(lector.MOTORES).index(cfg["motor"]), format_func=lector.MOTORES.get)
    c1, c2 = st.columns(2)
    url = c1.text_input("Dirección de Ollama", cfg["ollama_url"])
    vision = c2.text_input("Modelo Vision / facturas", cfg["ollama_vision_modelo"],
                           help="Recomendado: qwen3-vl:4b-instruct. En equipos muy justos: qwen3-vl:2b-instruct.")
    r1, r2 = st.columns(2)
    razon = r1.text_input("Modelo de razonamiento / asistente", cfg["ollama_razonamiento_modelo"],
                          help="Recomendado: qwen3:4b-instruct.")
    emb = r2.text_input("Modelo de embeddings / memoria", cfg["ollama_embedding_modelo"],
                        help="Recomendado: qwen3-embedding:0.6b. Solo indexa texto local, no envía documentos fuera.")
    ocr = st.checkbox("OCR en CPU para PDFs escaneados", cfg["ocr"])
    i1, i2 = st.columns(2)
    ia_siempre = i1.checkbox("Consultar la IA local en todas las facturas", cfg.get("ia_siempre", False),
                             help="Desactivado: usa reglas primero y Qwen3-VL solo cuando faltan datos, el PDF es escaneado o hay descuadres.")
    memoria = i2.checkbox("Aprendizaje / memoria de facturas aprobadas", cfg.get("memoria", True),
                          help="Guarda ejemplos validados en SQLite + Chroma local para reconocer formatos y patrones de proveedores/obras.")
    timeout = st.number_input("Tiempo máximo por lectura IA (s)", 60, 1800, int(cfg.get("timeout_ia", 420)), step=30)
    g1, g2 = st.columns(2)
    gpu = g1.checkbox("Usar la tarjeta gráfica", cfg["usar_gpu"],
                      help="Desactivado por defecto. Una GPU pequeña puede provocar que Ollama se cierre por falta de VRAM.")
    ctx = g2.selectbox("Contexto del modelo (tokens)", [4096, 6144, 8192, 12288], index=[4096, 6144, 8192, 12288].index(cfg["num_ctx"])
                       if cfg["num_ctx"] in (4096, 6144, 8192, 12288) else 2,
                       help="8192 es el valor equilibrado. Más contexto consume más RAM.")
    instalados = ollama_disponible(url)
    requeridos = [vision, razon, emb]
    if instalados:
        faltan = [m for m in requeridos if m and m not in instalados]
        st.success(f"Ollama activo. Modelos instalados: {', '.join(instalados)}")
        if faltan:
            st.warning("Faltan modelos recomendados: " + ", ".join(faltan) + ".\n\n" +
                       "Puedes instalarlos con: ollama pull " + " && ollama pull ".join(faltan))
    else:
        st.info("Ollama no está arrancado. La app sigue funcionando con OCR + reglas. Para el agente local instala Ollama y los modelos recomendados.")
    if st.button("Guardar motor de lectura", type="primary"):
        pares = (("motor_lectura", motor), ("ollama_url", url), ("ollama_modelo", vision),
                 ("ollama_vision_modelo", vision), ("ollama_razonamiento_modelo", razon),
                 ("ollama_embedding_modelo", emb), ("ocr", "1" if ocr else "0"),
                 ("ollama_gpu", "1" if gpu else "0"), ("ollama_ctx", str(ctx)),
                 ("ia_siempre", "1" if ia_siempre else "0"), ("kb_memoria", "1" if memoria else "0"),
                 ("timeout_ia", str(int(timeout))))
        for k, v in pares:
            db.set_setting(con, k, v)
        db.audit(con, usuario(), "config_motor_lectura", "ajustes", None,
                  {"motor": motor, "vision": vision, "razonamiento": razon, "embeddings": emb, "memoria": memoria}); con.commit()
        st.success("Guardado.")

    k1, k2 = st.columns(2)
    if k1.button("Probar Vision Qwen3-VL", help="Comprueba servidor y modelo Vision sin salir del equipo."):
        from core.ollama_cliente import diagnostico
        with st.spinner("Probando Qwen3-VL…"):
            res = diagnostico(url, vision, lector.opciones_ollama({"usar_gpu": gpu, "num_ctx": ctx}))
        for ok, msg in res:
            (st.success if ok else st.error)(msg)
    if k2.button("Reconstruir memoria local", help="Vuelve a indexar todas las facturas aprobadas. No modifica facturas."):
        from core import knowledge_base as kb
        with st.spinner("Indexando facturas aprobadas…"):
            n = kb.index_approved(con, url, emb)
        st.success(f"Memoria reconstruida: {n} documento(s).")

    st.subheader("IA en la nube (opcional)")
    st.caption("La API key se toma, por orden, de: esta sesión → variable de entorno ANTHROPIC_API_KEY → "
               ".streamlit/secrets.toml → guardada en la base de datos local.")
    k = st.text_input("API key de Anthropic", type="password", value=st.session_state.get("api_key", ""),
                      placeholder="sk-ant-…" if not api_key() else "(ya hay una configurada)")
    guardar = st.checkbox("Recordarla en este equipo (se guarda en la base de datos local)")
    mods = MODELOS_CLAUDE + ([modelo()] if modelo() not in MODELOS_CLAUDE else [])
    m = st.selectbox("Modelo", mods, index=mods.index(modelo()),
                     help="Sonnet: mejor equilibrio precisión/coste para facturas. Opus: máxima precisión en documentos difíciles. "
                          "Haiku: más barato, recomendable solo para documentos limpios.")
    otro = st.text_input("…u otro identificador de modelo", placeholder="dejar vacío para usar el de la lista")
    c1, c2 = st.columns(2)
    if c1.button("Guardar configuración de IA", type="primary"):
        if k:
            st.session_state["api_key"] = k
            if guardar:
                db.set_setting(con, "anthropic_api_key", k)
        st.session_state["modelo"] = otro.strip() or m
        db.set_setting(con, "modelo", otro.strip() or m)
        db.audit(con, usuario(), "config_ia", "ajustes", None, {"modelo": otro.strip() or m}); con.commit()
        st.success("Guardado.")
    if c2.button("Probar conexión", disabled=not (k or api_key())):
        try:
            import anthropic
            r = anthropic.Anthropic(api_key=k or api_key()).messages.create(
                model=otro.strip() or m, max_tokens=10, messages=[{"role": "user", "content": "Responde OK"}])
            st.success(f"Conexión correcta con {r.model}.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Fallo: {e}")
    if db.get_setting(con, "anthropic_api_key") and st.button("Borrar API key guardada"):
        db.set_setting(con, "anthropic_api_key", "")
        st.session_state.pop("api_key", None); st.rerun()

    st.subheader("Empresa receptora")
    nif = st.text_input("NIF de la empresa (se comprueba que cada factura venga a su nombre)",
                        db.get_setting(con, "empresa_nif", EMPRESA_NIF))
    ok, msg = validate_nif(nif)
    st.caption((" " if ok else " ") + msg)
    if st.button("Guardar NIF", disabled=not ok):
        db.set_setting(con, "empresa_nif", nif)
        db.audit(con, usuario(), "config_empresa", "ajustes", None, {"nif": nif}); con.commit()
        st.success("Guardado. Revalida los documentos desde  Incidencias.")

    st.subheader("Consumo de IA")
    u = db.one(con, "SELECT COUNT(*) n, COALESCE(SUM(tokens_entrada),0) ti, COALESCE(SUM(tokens_salida),0) tout "
                    "FROM documentos WHERE tokens_entrada IS NOT NULL")
    c = st.columns(3)
    c[0].metric("Documentos leídos por IA", u["n"])
    c[1].metric("Tokens de entrada", f"{u['ti']:,}".replace(",", "."))
    c[2].metric("Tokens de salida", f"{u['tout']:,}".replace(",", "."))
    st.caption("Consulta el precio vigente por token de cada modelo en https://docs.claude.com (sección de precios).")

    st.subheader("Auditoría")
    filtro = st.text_input("Filtrar (usuario, acción…)", key="aud_f")
    aud = pd.DataFrame(db.rows(con, "SELECT * FROM auditoria ORDER BY id DESC LIMIT 2000"))
    if not aud.empty and filtro:
        aud = aud[aud.apply(lambda r: filtro.lower() in " ".join(map(str, r.values)).lower(), axis=1)]
    st.dataframe(aud, hide_index=True, width="stretch", height=320)

    st.subheader("Copia de seguridad")
    st.caption(f"Base de datos: `{DB_PATH}` · PDFs originales en `{Path(DB_PATH).parent / 'pdfs'}`. La copia AUTOMÁTICA (diaria, verificada y "
               "en otro disco o NAS) se configura en «Automatizaciones → Copias de seguridad». Aquí se descarga una copia puntual.")
    if st.button("Generar copia de la base de datos"):
        tmp = Path(tempfile.gettempdir()) / "llorca_backup.db"
        if tmp.exists():
            tmp.unlink()
        dst = sqlite3.connect(tmp)
        con.backup(dst)   # copia consistente aunque la app esté en uso
        dst.close()
        st.session_state["backup"] = tmp.read_bytes()
    if st.session_state.get("backup"):
        st.download_button("Descargar copia", st.session_state["backup"],
                           file_name=f"llorca_facturas_{datetime.now():%Y%m%d_%H%M}.db")
