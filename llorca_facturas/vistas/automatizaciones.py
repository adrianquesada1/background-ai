"""Automatizaciones: buzón de facturas, correo saliente, SIS, copias de seguridad y tareas en segundo plano (solo administrador)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from core import db, credenciales, planificador, buzon, correo, sis, respaldo, actas_audio, maestros
from vistas.comun import get_con, usuario, rol


def _g(con, k, d=""):
    return db.get_setting(con, k, d)


def _guardar(con, pares: dict, accion: str):
    for k, v in pares.items():
        db.set_setting(con, k, "1" if v is True else "0" if v is False else str(v).strip())
    db.audit(con, usuario(), accion, "ajustes", None, {k: v for k, v in pares.items() if "clave" not in k and "secret" not in k})
    con.commit()


def _secreto(con, nombre: str, etiqueta: str, key: str):
    st.caption(f"{etiqueta}: {credenciales.origen(con, nombre)}")
    v = st.text_input(etiqueta + " (dejar vacío para no cambiarla)", type="password", key=key)
    return v


def render():
    con = get_con()
    st.title("Automatizaciones")
    if rol() != "admin":
        st.warning("Solo el administrador configura las automatizaciones.")
        return
    st.caption("Todo lo que la aplicación hace sola, sin que nadie esté delante: leer el buzón de facturas, enviar los correos aprobados, "
               "contabilizar en SIS y hacer la copia de seguridad. Cada tarea deja su resultado y sus errores aquí y en el centro de alertas.")
    t = st.tabs(["Estado", "Buzón de facturas", "Correo saliente", "SIS", "Copias de seguridad", "Otros"])
    with t[0]:
        _estado(con)
    with t[1]:
        _buzon(con)
    with t[2]:
        _correo(con)
    with t[3]:
        _sis(con)
    with t[4]:
        _copias(con)
    with t[5]:
        _otros(con)


def _estado(con):
    for x in planificador.estado(con):
        with st.container(border=True):
            a, b = st.columns([4, 1])
            icono = ":red[:material/error:]" if x["ultimo_error"] else ":green[:material/check_circle:]" if x["ultima_ejecucion"] else ":gray[:material/schedule:]"
            dur = f" · {x['duracion_s']} s" if x.get("duracion_s") is not None else ""
            ultima = (x["ultima_ejecucion"] or "nunca").replace("T", " ")
            a.markdown(f"{icono} **{x['titulo']}**  \n:gray[Última: {ultima}{dur}]  \n"
                       + (f":red[{x['ultimo_error']}]" if x["ultimo_error"] else (x["ultimo_resultado"] or "")))
            if b.button("Ejecutar ahora", key=f"aut_run_{x['nombre']}"):
                with st.spinner(x["titulo"] + "…"):
                    r = planificador.ejecutar_ahora(con, x["nombre"])
                (st.success if r["ok"] else st.error)(r["resultado"] or r["error"])
            with a.expander("Historial"):
                h = planificador.historial(con, x["nombre"], 30)
                if h:
                    st.dataframe(pd.DataFrame(h)[["inicio", "fin", "ok", "resultado", "error"]], hide_index=True, width="stretch")


def _buzon(con):
    cfg = buzon.config(con)
    st.caption("Recomendado: un buzón dedicado (p. ej. facturas@…) al que los proveedores envían sus facturas. Con Gmail use una "
               "contraseña de aplicación; con Microsoft 365, una aplicación registrada en Entra ID con permiso IMAP.AccessAsApp.")
    with st.form("aut_buzon"):
        activo = st.toggle("Leer el buzón automáticamente", cfg["activo"])
        c = st.columns([3, 1, 1.4, 1.4])
        host = c[0].text_input("Servidor IMAP", cfg["host"], placeholder="imap.gmail.com / outlook.office365.com")
        puerto = c[1].number_input("Puerto", 1, 65535, cfg["puerto"])
        seg = c[2].selectbox("Seguridad", ["ssl", "starttls", "ninguna"], ["ssl", "starttls", "ninguna"].index(cfg["seguridad"]))
        auth = c[3].selectbox("Acceso", ["clave", "m365"], ["clave", "m365"].index(cfg["auth"]),
                              format_func={"clave": "Usuario y contraseña", "m365": "Microsoft 365 (OAuth2)"}.get)
        c = st.columns(3)
        usu = c[0].text_input("Usuario / buzón", cfg["usuario"])
        carpeta = c[1].text_input("Carpeta a leer", cfg["carpeta"])
        proc = c[2].text_input("Mover los procesados a (opcional)", cfg["carpeta_procesados"], help="Debe existir en el buzón. Vacío: solo se marcan como leídos.")
        clave = _secreto(con, "buzon_clave", "Contraseña del buzón", "aut_bz_clave")
        c = st.columns(2)
        tenant = c[0].text_input("Microsoft 365: ID de inquilino (tenant)", cfg["tenant"])
        client = c[1].text_input("Microsoft 365: ID de aplicación (client id)", cfg["client_id"])
        secreto_m = _secreto(con, "buzon_client_secret", "Microsoft 365: secreto de la aplicación", "aut_bz_sec")
        c = st.columns(4)
        minutos = c[0].number_input("Cada (minutos)", 1, 1440, cfg["minutos"])
        dias = c[1].number_input("Correos de los últimos (días)", 1, 365, cfg["dias_atras"])
        solo = c[2].checkbox("Solo los no leídos", cfg["solo_no_leidos"])
        leer = c[3].checkbox("Leer las facturas al llegar", cfg["leer_auto"])
        c = st.columns(3)
        modo = c[0].selectbox("Uso de la IA local en la lectura", ["auto", "nunca", "siempre"], ["auto", "nunca", "siempre"].index(cfg["modo_ia"]),
                              format_func={"auto": "Solo cuando falten datos", "nunca": "Nunca (solo reglas)", "siempre": "Siempre"}.get)
        obras = maestros.listar_obras(con)
        ops = [0] + [o["id"] for o in obras]
        obra = c[1].selectbox("Obra por defecto (si la factura no la indica)", ops, ops.index(cfg["obra_defecto"] or 0) if (cfg["obra_defecto"] or 0) in ops else 0,
                              format_func=lambda i: "Detectarla en cada factura" if not i else next(f"{o['codigo']} · {o['nombre']}" for o in obras if o["id"] == i))
        soporte = c[2].checkbox("Importar también albaranes y certificaciones de proveedor (como documentos soporte)", cfg["soporte"])
        ign = st.text_input("Remitentes o dominios a ignorar (separados por comas)", ", ".join(cfg["ignorar"]), placeholder="boletin@…, publicidad.com")
        if st.form_submit_button("Guardar buzón", type="primary"):
            _guardar(con, {"buzon_activo": activo, "buzon_host": host, "buzon_puerto": int(puerto), "buzon_seguridad": seg, "buzon_auth": auth,
                           "buzon_usuario": usu, "buzon_carpeta": carpeta or "INBOX", "buzon_carpeta_procesados": proc, "buzon_tenant": tenant,
                           "buzon_client_id": client, "buzon_minutos": int(minutos), "buzon_dias_atras": int(dias), "buzon_solo_no_leidos": solo,
                           "buzon_leer_auto": leer, "buzon_modo_ia": modo, "buzon_obra_defecto": obra or "", "buzon_soporte": soporte,
                           "buzon_ignorar": ign}, "config_buzon")
            if clave:
                credenciales.guardar(con, "buzon_clave", clave, usuario())
            if secreto_m:
                credenciales.guardar(con, "buzon_client_secret", secreto_m, usuario())
            st.success("Guardado.")
    if st.button("Probar conexión con el buzón"):
        ok, msg = buzon.probar_conexion(con)
        (st.success if ok else st.error)(msg)


def _correo(con):
    cfg = correo.config(con)
    st.caption("Los correos que prepara la aplicación (reclamaciones, peticiones de oferta, autorizaciones de facturación, avisos de pago, "
               "CAE…) esperan en la bandeja de salida. En modo PRUEBA no sale nada: se guardan como .eml para revisarlos.")
    with st.form("aut_smtp"):
        modo = st.radio("Envío", ["desactivado", "prueba", "real"], ["desactivado", "prueba", "real"].index(cfg["modo"]), horizontal=True,
                        format_func={"desactivado": "Desactivado", "prueba": "Prueba (no envía)", "real": "Real"}.get)
        c = st.columns([3, 1, 1.4, 1.4])
        host = c[0].text_input("Servidor SMTP", cfg["host"], placeholder="smtp.gmail.com / smtp.office365.com")
        puerto = c[1].number_input("Puerto", 1, 65535, cfg["puerto"])
        seg = c[2].selectbox("Seguridad", ["starttls", "ssl", "ninguna"], ["starttls", "ssl", "ninguna"].index(cfg["seguridad"]))
        auth = c[3].selectbox("Acceso", ["clave", "m365", "ninguno"], ["clave", "m365", "ninguno"].index(cfg["auth"]),
                              format_func={"clave": "Usuario y contraseña", "m365": "Microsoft 365 (OAuth2)", "ninguno": "Sin autenticación (relay interno)"}.get)
        c = st.columns(3)
        usu = c[0].text_input("Usuario", cfg["usuario"])
        rem = c[1].text_input("Remitente (From)", cfg["remitente"])
        nom = c[2].text_input("Nombre del remitente", cfg["nombre"])
        clave = _secreto(con, "smtp_clave", "Contraseña SMTP", "aut_sm_clave")
        c = st.columns(2)
        tenant = c[0].text_input("Microsoft 365: tenant", cfg["tenant"])
        client = c[1].text_input("Microsoft 365: client id", cfg["client_id"])
        secreto_m = _secreto(con, "smtp_client_secret", "Microsoft 365: secreto de la aplicación", "aut_sm_sec")
        c = st.columns(2)
        resp = c[0].text_input("Responder a (opcional)", cfg["responder_a"])
        cco = c[1].text_input("Copia oculta de todo lo enviado (opcional, archivo)", cfg["copia_oculta"])
        firma = st.text_area("Firma", cfg["firma"], height=80)
        auto = st.multiselect("Tipos de correo que salen SIN aprobación", list(correo.TIPOS), sorted(cfg["auto"]), format_func=correo.TIPOS.get)
        c = st.columns(2)
        cuatro = c[0].checkbox("Cuatro ojos: no aprueba quien prepara el correo", cfg["cuatro_ojos"])
        avisos = c[1].checkbox("Preparar aviso de pago a cada proveedor al confirmar una remesa", _g(con, "correo_avisos_pago", "0") == "1")
        if st.form_submit_button("Guardar correo saliente", type="primary"):
            _guardar(con, {"smtp_modo": modo, "smtp_host": host, "smtp_puerto": int(puerto), "smtp_seguridad": seg, "smtp_auth": auth, "smtp_usuario": usu,
                           "smtp_remitente": rem, "smtp_nombre": nom, "smtp_tenant": tenant, "smtp_client_id": client, "smtp_responder_a": resp,
                           "smtp_copia_oculta": cco, "smtp_firma": firma, "smtp_auto_tipos": ",".join(auto), "smtp_cuatro_ojos": cuatro,
                           "correo_avisos_pago": avisos}, "config_correo")
            if clave:
                credenciales.guardar(con, "smtp_clave", clave, usuario())
            if secreto_m:
                credenciales.guardar(con, "smtp_client_secret", secreto_m, usuario())
            st.success("Guardado.")
    if st.button("Probar conexión SMTP"):
        ok, msg = correo.probar_conexion(con)
        (st.success if ok else st.error)(msg)


def _sis(con):
    cfg = sis.config(con)
    st.caption("Envía el asiento de cada factura aprobada a SIS por su API. Empiece en modo PRUEBA: se generan los JSON en "
               f"`{sis.carpeta_prueba()}` para validarlos con el proveedor de SIS antes de activar el envío real. "
               "El Excel/CSV de asientos sigue disponible como alternativa.")
    with st.form("aut_sis"):
        modo = st.radio("Envío a SIS", ["desactivado", "prueba", "real"], ["desactivado", "prueba", "real"].index(cfg["modo"]), horizontal=True,
                        format_func={"desactivado": "Desactivado", "prueba": "Prueba (no envía)", "real": "Real"}.get)
        c = st.columns([3, 2])
        url = c[0].text_input("Dirección de la API", cfg["url"], placeholder="https://sis.miempresa.local")
        ruta = c[1].text_input("Ruta de asientos", cfg["ruta"])
        c = st.columns(3)
        auth = c[0].selectbox("Autenticación", ["bearer", "basic", "cabecera"], ["bearer", "basic", "cabecera"].index(cfg["auth"]),
                              format_func={"bearer": "Token (Bearer)", "basic": "Usuario y contraseña", "cabecera": "Clave en cabecera"}.get)
        usu = c[1].text_input("Usuario (si Basic)", cfg["usuario"])
        cab = c[2].text_input("Nombre de la cabecera (si clave en cabecera)", cfg["cabecera_clave"])
        clave = _secreto(con, "sis_clave", "Token / contraseña / clave de SIS", "aut_sis_clave")
        c = st.columns(4)
        emp = c[0].text_input("Código de empresa en SIS", cfg["empresa"])
        dia = c[1].text_input("Diario", cfg["diario"])
        desde = c[2].text_input("Enviar facturas aprobadas desde (AAAA-MM-DD)", cfg["desde"], help="Lo anterior ya está contabilizado en SIS: no se envía.")
        mins = c[3].number_input("Cada (minutos)", 1, 1440, cfg["minutos"])
        c = st.columns(3)
        campo_id = c[0].text_input("Campo con el id del asiento en la respuesta", cfg["campo_id"])
        pdf = c[1].checkbox("Adjuntar el PDF de la factura (base64)", cfg["adjuntar_pdf"])
        tls = c[2].checkbox("Verificar el certificado HTTPS de SIS", cfg["verificar_tls"])
        mapeo = st.text_area("Mapeo de campos (JSON: nombre interno → nombre en la API de SIS)", json.dumps(cfg["mapeo"], ensure_ascii=False, indent=1),
                             height=160)
        if st.form_submit_button("Guardar SIS", type="primary"):
            try:
                m = json.loads(mapeo)
                assert isinstance(m, dict)
            except Exception:
                st.error("El mapeo no es un JSON válido.")
            else:
                _guardar(con, {"sis_modo": modo, "sis_url": url, "sis_ruta": ruta, "sis_auth": auth, "sis_usuario": usu, "sis_cabecera_clave": cab,
                               "sis_empresa": emp, "sis_diario": dia, "sis_desde": desde, "sis_minutos": int(mins), "sis_campo_id": campo_id,
                               "sis_adjuntar_pdf": pdf, "sis_verificar_tls": tls, "sis_mapeo": json.dumps(m, ensure_ascii=False)}, "config_sis")
                if clave:
                    credenciales.guardar(con, "sis_clave", clave, usuario())
                st.success("Guardado.")
    if st.button("Probar conexión con SIS"):
        ok, msg = sis.probar_conexion(con)
        (st.success if ok else st.error)(msg)


def _copias(con):
    cfg = respaldo.config(con)
    for a in respaldo.avisos(con):
        st.warning(a)
    u = respaldo.ultima_ok(con)
    if u:
        st.success(f"Última copia correcta: {u['fin'].replace('T', ' ')} en «{u['destino']}» ({(u['bytes'] or 0) / 1e6:.1f} MB, verificada).")
    with st.form("aut_copias"):
        activo = st.toggle("Copia de seguridad automática", cfg["activo"])
        dest = st.text_area("Destinos (uno por línea): otro disco, NAS o carpeta de red, carpeta sincronizada con la nube",
                            "\n".join(cfg["destinos"]), placeholder="D:\\CopiasLlorca\n\\\\NAS\\copias\\llorca", height=90)
        c = st.columns(5)
        hora = c[0].text_input("Hora diaria (HH:MM)", cfg["hora"])
        cada = c[1].number_input("Además cada N horas (0 = no)", 0, 24, cfg["cada_horas"])
        di = c[2].number_input("Diarias a conservar", 1, 365, cfg["diarias"])
        se = c[3].number_input("Semanales", 0, 104, cfg["semanales"])
        me = c[4].number_input("Mensuales", 0, 120, cfg["mensuales"])
        arch = st.checkbox("Copiar también los archivos (PDF, justificantes, planos, audios…)", cfg["archivos"])
        if st.form_submit_button("Guardar copias", type="primary"):
            _guardar(con, {"respaldo_activo": activo, "respaldo_destinos": ";".join(x.strip() for x in dest.splitlines() if x.strip()),
                           "respaldo_hora": hora, "respaldo_cada_horas": int(cada), "respaldo_diarias": int(di), "respaldo_semanales": int(se),
                           "respaldo_mensuales": int(me), "respaldo_archivos": arch}, "config_copias")
            st.success("Guardado.")
    if st.button("Hacer una copia ahora", type="primary"):
        with st.spinner("Copiando y verificando…"):
            r = planificador.ejecutar_ahora(con, "respaldo")
        (st.success if r["ok"] else st.error)(r["resultado"] or r["error"])
    st.markdown("**Copias disponibles y simulacro de restauración**")
    for d in cfg["destinos"]:
        cps = respaldo.listar_copias(d) if Path(d).exists() else []
        st.caption(f"{d}: {len(cps)} copia(s)")
        if cps:
            sel = st.selectbox("Copia", [c["archivo"] for c in cps], key=f"aut_cp_{d}",
                               format_func=lambda a: next(f"{c['fecha']:%d/%m/%Y %H:%M} · {c['mb']} MB" for c in cps if c["archivo"] == a))
            if st.button("Simulacro de restauración", key=f"aut_sim_{d}", help="Descomprime la copia en temporal, comprueba su integridad y "
                                                                             "sus recuentos. No toca los datos en uso."):
                ok, det = respaldo.simulacro(sel)
                (st.success if ok else st.error)(("La copia se puede restaurar: " if ok else "La copia NO sirve: ") + det)
    st.info("Para restaurar de verdad: pare la aplicación y ejecute `python restaurar_copia.py <archivo .db.gz>` en la carpeta de la aplicación. "
            "La base de datos actual se conserva al lado con el sufijo «antes_de_restaurar». Los archivos se recuperan copiando la carpeta "
            "«archivos» del destino a la carpeta «data».")
    hist = db.rows(con, "SELECT inicio, destino, ok, bytes, archivos_copiados, borradas, error FROM respaldos ORDER BY id DESC LIMIT 60")
    if hist:
        with st.expander("Historial de copias"):
            st.dataframe(pd.DataFrame(hist), hide_index=True, width="stretch")


def _otros(con):
    st.markdown("**Transcripción de actas (audio de reuniones)**")
    motor, msg = actas_audio.motor_disponible()
    (st.success if motor else st.warning)(msg)
    c = st.columns(3)
    mod = c[0].selectbox("Modelo Whisper", ["tiny", "base", "small", "medium", "large-v3"],
                         ["tiny", "base", "small", "medium", "large-v3"].index(_g(con, "whisper_modelo", "small")),
                         help="small: buen equilibrio en CPU (≈ la duración del audio). medium/large: más preciso, bastante más lento.")
    resum = c[1].checkbox("Resumen del acta con la IA local (Ollama)", _g(con, "actas_resumen_ia", "1") == "1")
    st.markdown("**Pagos y subcontratas**")
    c2 = st.columns(2)
    aeat = c2[0].checkbox("Bloquear el pago a subcontratas sin certificado de estar al corriente con la AEAT (art. 43.1.f LGT)",
                          _g(con, "pagos_bloquear_aeat", "0") == "1", help="Desactivado: solo se avisa en la remesa.")
    cp4 = c2[1].checkbox("Cuatro ojos en certificaciones de subcontrata (no aprueba quien mide)", _g(con, "cp_cuatro_ojos", "1") == "1")
    c3 = st.columns(3)
    ret = c3[0].text_input("Retención de garantía por defecto a subcontratas (%)", _g(con, "cp_ret_defecto", "5"))
    dias_df = c3[1].number_input("Aviso de planos sin respuesta de la DF (días)", 1, 120, int(_g(con, "planos_dias_df", "10")))
    dias_res = c3[2].number_input("Aviso de residuos sin certificado del gestor (días)", 1, 365, int(_g(con, "residuos_dias_certificado", "30")))
    c4 = st.columns(2)
    auto_conc = c4[0].checkbox("Conciliación bancaria: aplicar solas las casaciones inequívocas", _g(con, "conc_auto", "1") == "1")
    nivel = c4[1].number_input("CAE: nivel máximo de subcontratación", 1, 5, int(_g(con, "cae_nivel_max", "3")))
    if st.button("Guardar", type="primary", key="aut_otros_g"):
        _guardar(con, {"whisper_modelo": mod, "actas_resumen_ia": resum, "pagos_bloquear_aeat": aeat, "cp_cuatro_ojos": cp4, "cp_ret_defecto": ret,
                       "planos_dias_df": int(dias_df), "residuos_dias_certificado": int(dias_res), "conc_auto": auto_conc,
                       "cae_nivel_max": int(nivel)}, "config_otros")
        st.success("Guardado.")
