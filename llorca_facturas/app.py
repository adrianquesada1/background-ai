"""
Llorca Group · Control de facturas por obra
Ejecutar:  streamlit run app.py
"""
import logging
import streamlit as st

from pathlib import Path  # noqa: E402

# pdfminer (lo usa pdfplumber) avisa de fuentes mal definidas en algunos PDF de proveedores
# ("Could not get FontBBox..."). Es inofensivo: el texto se extrae igual. Se silencia.
for _lg in ("pdfminer", "pdfminer.pdffont", "pdfminer.pdfpage", "pdfminer.pdfinterp"):
    logging.getLogger(_lg).setLevel(logging.ERROR)


def _ejecutar_js(codigo: str) -> None:
    """Ejecuta un <script> en la página (cookies de sesión) sin usar st.components.v1.html (obsoleto)."""
    try:
        st.html(codigo, unsafe_allow_javascript=True)
    except TypeError:                          # Streamlit antiguo sin ese parámetro
        import streamlit.components.v1 as _cmp
        _cmp.html(codigo, height=0)

BASE = Path(__file__).resolve().parent
st.set_page_config(page_title="Llorca Group · Control económico de obra", page_icon=str(BASE / "assets" / "icono_llorca.png"),
                   layout="wide")

from vistas import comun  # noqa: E402
from vistas import (panel, ingesta, revision, documentos, obras, proveedores, incidencias, asistente, ajustes,  # noqa: E402
                    inicio, certificaciones, rentabilidad, contratacion, auditoria, buscar, deshacer, pendientes, avales, gobierno, informes, internos, tesoreria, estudios, casacion, mando, cierre, pagos)
from core import db  # noqa: E402

from vistas import estilo  # noqa: E402

estilo.aplicar()
con = comun.get_con()

# --------------------------------------------------------------------------- estado de sesión persistente
# Streamlit borra el valor de un control cuando se cambia de página. Reasignarlo al empezar cada ejecución lo conserva
# (filtros, obra seleccionada, documento en revisión…). SOLO para selectores/filtros: los botones, descargas, cargas de
# archivo y editores de tabla no admiten que se les asigne valor.
import re as _re  # noqa: E402
_PERSISTIR = _re.compile(r"^(buscar_q|ing_modo_ia|ing_obra_subida|ing_forzar|chat_redactar|panel_(obra|desde|hasta|alc|part_det|hp)|doc_(obra|est|tipo|txt)|rev_(estados|sel)|aud_(obra|cert)|"
                         r"inc_obra|ing_(modo|obra_lote)|cert_(obra|res|exp|q|estr)|cmp_(a|b|pid)|con_obra|rent_obra|ini_obra|"
                         r"int_(obra|tipo|f_tipo|f_est|ver_anul)|cie_obra|pag_obra|tes_(obra|saldo)|est_sel|cas_obra|aud_crit_q|aud_dev|obras_sel|obras_vista|of_(est|prov|desde|hasta)|gob_obra|inf_(obra|cert|dest)|aval_obra|chat_redactar)$")
for _k in list(st.session_state.keys()):
    if isinstance(_k, str) and _PERSISTIR.match(_k):
        try:
            st.session_state[_k] = st.session_state[_k]
        except Exception:
            pass

# --------------------------------------------------------------------------- acceso
from core import sesiones  # noqa: E402
if not st.session_state.get("auth"):
    try:
        _tok = st.context.cookies.get(sesiones.COOKIE)
    except Exception:
        _tok = None
    _ses = sesiones.validar(con, _tok)
    if _ses:                                   # recarga de página o nueva pestaña: sesión recuperada
        st.session_state["auth"] = _ses
        st.session_state["usuario"] = _ses["usuario"]
        st.session_state["_token"] = _tok
if not st.session_state.get("auth"):
    from vistas import acceso
    acceso.pantalla(con)
    st.stop()
if st.session_state.get("_cookie_js"):
    _ejecutar_js(st.session_state.pop("_cookie_js"))
elif st.session_state.get("_token") and not sesiones.validar(con, st.session_state["_token"]):
    # sesión cerrada por el administrador o usuario desactivado
    for _k in list(st.session_state.keys()):
        del st.session_state[_k]
    st.rerun()
st.logo(str(BASE / "assets" / "logo_llorca.png"), size="large")
ROL = st.session_state["auth"]["rol"]

P = {
    "inicio": st.Page(inicio.render, title="Inicio", icon=":material/home:", url_path="inicio", default=True),
    "buscar": st.Page(buscar.render, title="Buscar", icon=":material/search:", url_path="buscar"),
    "pendientes": st.Page(pendientes.render, title="Mis pendientes", icon=":material/inbox:", url_path="pendientes"),
    "tesoreria": st.Page(tesoreria.render, title="Tesorería y cobros", icon=":material/account_balance:", url_path="tesoreria"),
    "estudios": st.Page(estudios.render, title="Estudios y ofertas", icon=":material/request_quote:", url_path="estudios"),
    "casacion": st.Page(casacion.render, title="Casación y excepciones", icon=":material/rule:", url_path="casacion"),
    "cierre": st.Page(cierre.render, title="Cierre mensual", icon=":material/lock_clock:", url_path="cierre"),
    "pagos": st.Page(pagos.render, title="Pagos y remesas", icon=":material/payments:", url_path="pagos"),
    "mando": st.Page(mando.render, title="Cuadro de mando", icon=":material/monitoring:", url_path="mando"),
    "internos": st.Page(internos.render, title="Costes y ventas internos", icon=":material/engineering:", url_path="internos"),
    "gobierno": st.Page(gobierno.render, title="Diario de obra", icon=":material/event_note:", url_path="diario"),
    "informes": st.Page(informes.render, title="Informes mensuales", icon=":material/summarize:", url_path="informes"),
    "avales": st.Page(avales.render, title="Avales y garantías", icon=":material/verified_user:", url_path="avales"),
    "auditoria": st.Page(auditoria.render, title="Rentabilidad por oficio", icon=":material/query_stats:", url_path="rentabilidad"),
    "rentabilidad": st.Page(rentabilidad.render, title="Venta y coste por capítulo", icon=":material/trending_up:", url_path="capitulos"),
    "panel": st.Page(panel.render, title="Coste de obra", icon=":material/bar_chart:", url_path="panel"),
    "certificaciones": st.Page(certificaciones.render, title="Certificaciones", icon=":material/description:", url_path="certificaciones"),
    "contratacion": st.Page(contratacion.render, title="Contratación", icon=":material/handshake:", url_path="contratacion"),
    "asistente": st.Page(asistente.render, title="Asistente IA", icon=":material/forum:", url_path="asistente"),
    "ingesta": st.Page(ingesta.render, title="Entrada de facturas", icon=":material/upload_file:", url_path="ingesta"),
    "revision": st.Page(revision.render, title="Revisión y aprobación", icon=":material/fact_check:", url_path="revision"),
    "documentos": st.Page(documentos.render, title="Documentos y pagos", icon=":material/receipt_long:", url_path="documentos"),
    "deshacer": st.Page(deshacer.render, title="Papelera y deshacer", icon=":material/history:", url_path="deshacer"),
    "incidencias": st.Page(incidencias.render, title="Incidencias", icon=":material/report:", url_path="incidencias"),
    "obras": st.Page(obras.render, title="Obras y partidas", icon=":material/apartment:", url_path="obras"),
    "proveedores": st.Page(proveedores.render, title="Proveedores", icon=":material/business:", url_path="proveedores"),
    "ajustes": st.Page(ajustes.render, title="Configuración y auditoría", icon=":material/settings:", url_path="ajustes"),
}
st.session_state["_paginas"] = P
st.session_state["_pagina_revision"] = P["revision"]

CONTROL = [P["auditoria"], P["rentabilidad"], P["panel"], P["internos"], P["certificaciones"], P["contratacion"], P["gobierno"],
           P["informes"], P["asistente"]]
CIRCUITO = [P["ingesta"], P["revision"], P["documentos"], P["casacion"], P["incidencias"], P["deshacer"]]
FINANZAS = [P["tesoreria"], P["pagos"], P["cierre"], P["avales"], P["mando"]]
if ROL == "admin":
    secciones = {"": [P["inicio"], P["pendientes"], P["buscar"]], "Control de obra": CONTROL, "Circuito de facturas": CIRCUITO,
                 "Tesorería y finanzas": FINANZAS, "Estudios": [P["estudios"]], "Maestros": [P["obras"], P["proveedores"], P["ajustes"]]}
elif ROL in ("gestor", "direccion"):
    secciones = {"": [P["inicio"], P["pendientes"], P["buscar"]], "Control de obra": CONTROL, "Circuito de facturas": CIRCUITO,
                 "Tesorería y finanzas": FINANZAS, "Estudios": [P["estudios"]], "Maestros": [P["obras"], P["proveedores"]]}
elif ROL == "jefe_obra":        # solo sus obras
    secciones = {"": [P["inicio"], P["pendientes"], P["buscar"]],
                 "Mis obras": [P["auditoria"], P["rentabilidad"], P["panel"], P["internos"], P["certificaciones"], P["contratacion"],
                               P["gobierno"], P["informes"], P["asistente"]],
                 "Facturas": [P["revision"], P["documentos"], P["incidencias"]]}
elif ROL == "tecnico":
    secciones = {"": [P["inicio"], P["buscar"]], "Estudios": [P["estudios"]],
                 "Control de obra": [P["certificaciones"], P["contratacion"], P["gobierno"], P["rentabilidad"], P["panel"], P["asistente"]],
                 "Maestros": [P["obras"]]}
else:                           # consulta: solo lectura
    secciones = {"": [P["inicio"], P["buscar"]],
                 "Control de obra": [P["auditoria"], P["rentabilidad"], P["panel"], P["certificaciones"], P["asistente"]],
                 "Documentos": [P["documentos"]]}

# --------------------------------------------------------------------------- preferencias del usuario
# Filtros, obra seleccionada, opciones de lectura… se guardan por usuario en la base de datos: al volver a entrar
# (otro día, otro navegador u otro equipo) cada persona encuentra la aplicación como la dejó.
_uid = st.session_state["auth"]["id"]
if not st.session_state.get("_prefs_cargadas"):
    for _k, _v in comun.usuarios.cargar_preferencias(con, _uid).items():
        if _PERSISTIR.match(_k) and _k not in st.session_state:
            st.session_state[_k] = _v
    st.session_state["_prefs_cargadas"] = True
else:
    comun.usuarios.guardar_preferencias(con, _uid, {k: v for k, v in st.session_state.items()
                                                    if isinstance(k, str) and _PERSISTIR.match(k)})

nav = st.navigation(secciones, expanded=20)
sesiones.registrar_pagina(con, st.session_state.get("_token"), nav.title)

with st.sidebar:
    a_ = st.session_state["auth"]
    st.markdown(f"**{a_['nombre']}**  \n:gray[{a_['usuario']} · {comun.usuarios.ROLES.get(a_['rol'], a_['rol'])}]")
    if st.button("Cerrar sesión", icon=":material/logout:", width="stretch"):
        db.audit(con, a_["usuario"], "cierre_sesion", "usuario", a_["id"], None); con.commit()
        sesiones.cerrar(con, st.session_state.get("_token"))
        for k_ in list(st.session_state.keys()):
            del st.session_state[k_]
        _ejecutar_js(sesiones.js_cookie(None))
        st.session_state["_salida"] = True
        st.rerun()
    st.divider()

    @st.fragment(run_every=4)
    def _estado_global():
        from core import trabajos, lector
        t = trabajos.activo(con)
        if t:
            pct = round(100 * t["hechos"] / max(1, t["total"]))
            st.progress(pct / 100, text=f"Leyendo facturas: {pct} % ({t['hechos']} de {t['total']})")
        n = db.one(con, """SELECT
            SUM(estado='sin_procesar') AS sp, SUM(estado='pendiente_revision') AS pr, SUM(estado='duplicado') AS du,
            (SELECT COUNT(*) FROM incidencias i JOIN documentos d ON d.id=i.documento_id
              WHERE i.resuelta=0 AND i.severidad='critica' AND d.estado NOT IN ('rechazada','duplicado')) AS crit
            FROM documentos""") or {}
        try:
            from core import alertas as _al
            _n_al = len(_al.calcular(con, ROL, comun.obras_permitidas()))
            if _n_al:
                st.markdown(f":red[**{_n_al} asunto(s) requieren acción** (ver Inicio)]")
        except Exception:
            pass
        st.markdown(f"**Pendiente de lectura:** {n.get('sp') or 0}  \n**Pendiente de revisión:** {n.get('pr') or 0}  \n"
                    f"**Incidencias críticas:** {n.get('crit') or 0}  \n**Duplicados apartados:** {n.get('du') or 0}")
        st.caption("Lectura de documentos: local (sin envío a terceros)" if lector.config(con)["motor"] != "claude"
                   else "Lectura de documentos: API externa")
    _estado_global()

nav.run()
