"""Inicio: estado de cada obra y siguiente paso recomendado, en una sola pantalla."""
from __future__ import annotations

import streamlit as st

from core import db, maestros, obra_control as oc, analytics
from core.money import fmt_eur, from_cents
from vistas.comun import get_con, selector_obra


def _ir(nombre: str, etiqueta: str, key: str):
    pags = st.session_state.get("_paginas", {})
    if nombre in pags:
        st.page_link(pags[nombre], label=etiqueta, icon=":material/arrow_forward:")


def _paso(ok: bool | None, titulo: str, detalle: str, destino: str, etiqueta: str, key: str):
    icono = ":green[:material/check_circle:]" if ok else (":gray[:material/schedule:]" if ok is None else ":gray[:material/radio_button_unchecked:]")
    with st.container(border=True):
        a, b = st.columns([5, 2])
        a.markdown(f"{icono} **{titulo}**  \n{detalle}")
        with b:
            _ir(destino, etiqueta, key)


def _hoy(con):
    """Lo que requiere acción hoy, según el cargo y las obras de cada persona."""
    from core import alertas
    from vistas.comun import rol as _rol, obras_permitidas as _op
    al = alertas.calcular(con, _rol(), _op())
    pags = st.session_state.get("_paginas", {})
    with st.container(border=True):
        st.markdown(f"**Hoy** · {len(al)} asunto(s) que requieren acción" if al else "**Hoy** · nada pendiente que requiera acción.")
        for a in al:
            icono = {1: ":red[:material/error:]", 2: ":orange[:material/warning:]", 3: ":gray[:material/info:]"}[a["prioridad"]]
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"{icono} **{a['area']}** · {a['texto']}")
            if a["pagina"] in pags:
                c2.page_link(pags[a["pagina"]], label="Ir", icon=":material/arrow_forward:")


def render():
    con = get_con()
    _hoy(con)
    st.title("Control de obra")
    obra_id = selector_obra("ini_obra", permitir_todas=False)
    if not obra_id:
        return
    obra = db.one(con, "SELECT * FROM obras WHERE id=?", (obra_id,))
    cert = oc.ultima_cert(con, obra_id)
    n = db.one(con, """SELECT COUNT(*) total, SUM(estado='sin_procesar') sp, SUM(estado='pendiente_revision') pr,
                       SUM(estado='revisada') rv, SUM(estado='aprobada') ap FROM documentos WHERE obra_id=? OR obra_id IS NULL""",
               (obra_id,))
    crit = db.one(con, """SELECT COUNT(*) n FROM incidencias i JOIN documentos d ON d.id=i.documento_id
                          WHERE i.resuelta=0 AND i.severidad IN ('critica','alta') AND d.estado<>'rechazada'
                          AND (d.obra_id=? OR d.obra_id IS NULL)""", (obra_id,))["n"]
    estructura = any((p.get("origen_estructura") or "").startswith("cert:") for p in maestros.partidas_de_obra(con, obra_id))
    n_of = db.one(con, "SELECT COUNT(*) n FROM ofertas WHERE obra_id=?", (obra_id,))["n"]
    n_cm = db.one(con, "SELECT COUNT(*) n FROM costes_manuales WHERE obra_id=?", (obra_id,))["n"]
    r = analytics.resumen(con, obra_id=obra_id)

    # --------------------------------------------------------------- cifras clave
    k = st.columns(4)
    if cert:
        df = oc.rentabilidad(con, obra_id, cert["id"], "mes")
        venta_mes, coste_mes = int(df["certificado_m"].sum()), int(df["coste_m"].sum())
        k[0].metric(f"Certificado a origen (nº {cert['numero']})", fmt_eur(oc.m2d(cert["total_origen_m"])))
        k[1].metric("Certificado del mes", fmt_eur(oc.m2d(venta_mes)))
        k[2].metric("Coste del mes (facturas cargadas)", fmt_eur(oc.m2d(coste_mes)))
        k[3].metric("Margen del mes", fmt_eur(oc.m2d(venta_mes - coste_mes)),
                    f"{(venta_mes - coste_mes) / venta_mes * 100:.1f} %".replace(".", ",") if venta_mes else None)
        st.caption(f"{obra['codigo']} · {obra['nombre']} · cliente {cert['cliente'] or obra['cliente'] or '—'} · "
                   f"certificación de {cert['fecha']}. El margen del mes solo es fiable cuando están cargadas TODAS las "
                   f"facturas del periodo.")
    else:
        k[0].metric("Coste cargado", fmt_eur(r.get("base", 0)) if r.get("n_docs") else "—")
        k[1].metric("Documentos", r.get("n_docs", 0))
        st.caption("Importa la certificación para ver venta y margen.")

    st.subheader("Siguientes pasos")
    _paso(bool(cert), "1 · Certificación del cliente",
          f"Nº {cert['numero']} del {cert['fecha']} importada y {'verificada' if cert['verificada'] else 'con avisos'}."
          if cert else "Importa el PDF de la certificación mensual: es la VENTA contra la que se mide todo.",
          "certificaciones", "Certificaciones", "p1")
    _paso(estructura if cert else None, "2 · Estructura de coste = capítulos de la certificación",
          "El coste se imputa con los mismos capítulos con los que se vende." if estructura else
          "Adopta los capítulos de la certificación como partidas de coste (pestaña «Estructura de coste»).",
          "certificaciones", "Estructura de coste", "p2")
    _paso((n["total"] or 0) > 0 and not n["sp"], "3 · Facturas registradas y leídas",
          f"{n['total'] or 0} documentos · {n['sp'] or 0} sin leer · {n['pr'] or 0} por revisar · {n['ap'] or 0} aprobados.",
          "ingesta", "Entrada de facturas", "p3")
    _paso(crit == 0 and (n["total"] or 0) > 0, "4 · Revisión e incidencias",
          f"{crit} incidencia(s) críticas/altas abiertas." if crit else "Sin incidencias graves abiertas.",
          "revision", "Revisión", "p4")
    _paso(n_of > 0, "5 · Ofertas y contratos por capítulo",
          f"{n_of} oferta(s) registradas." if n_of else "Registra ofertas y adjudicaciones para medir margen previsto y consumo de contratos.",
          "contratacion", "Contratación", "p5")
    _paso(n_cm > 0, "6 · Coste a origen (SIS) y costes propios",
          f"{n_cm} registro(s) de coste manual/importado." if n_cm else
          "Opcional: importa el coste a origen desde SIS para ver el margen a origen de toda la obra.",
          "rentabilidad", "Venta y coste por capítulo", "p6")

    n_aud = db.one(con, "SELECT COUNT(*) n FROM compras_sis WHERE obra_id=?", (obra_id,))["n"]
    _paso(n_aud > 0, "7 · Coste a origen de SIS (opcional)",
          f"{n_aud} facturas de compra de SIS cargadas: el resultado a origen es completo." if n_aud else
          "Con la exportación de compras y partes de trabajo de SIS, la rentabilidad por oficio a origen es completa.",
          "auditoria", "Rentabilidad por oficio", "p7")

    with st.expander("Mis preferencias"):
        st.caption("La aplicación recuerda por usuario los filtros, la obra seleccionada y sus opciones, en cualquier equipo. "
                   "Si algo se queda en un estado raro, puede volver a los valores por defecto.")
        if st.button("Restablecer mis preferencias"):
            from core import usuarios as U
            U.borrar_preferencias(con, (st.session_state.get("auth") or {}).get("id"))
            auth_, tok_ = st.session_state.get("auth"), st.session_state.get("_token")
            for k_ in list(st.session_state.keys()):
                del st.session_state[k_]
            st.session_state["auth"], st.session_state["_token"] = auth_, tok_
            st.session_state["usuario"] = auth_["usuario"] if auth_ else None
            st.rerun()

    with st.expander("Mi cuenta: cambiar contraseña"):
        from core import usuarios as U
        with st.form("mi_clave", clear_on_submit=True):
            a_ = st.session_state.get("auth") or {}
            actual = st.text_input("Contraseña actual", type="password")
            n1 = st.text_input("Nueva contraseña", type="password")
            n2 = st.text_input("Repetir nueva contraseña", type="password")
            if st.form_submit_button("Cambiar contraseña"):
                ok_, err = U.autenticar(con, a_.get("usuario", ""), actual)
                if not ok_:
                    st.error("La contraseña actual no es correcta.")
                elif n1 != n2:
                    st.error("Las contraseñas nuevas no coinciden.")
                else:
                    try:
                        U.cambiar_clave(con, a_["id"], n1, a_["usuario"])
                        st.success("Contraseña cambiada.")
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))

    with st.expander("¿Cómo encaja todo?"):
        st.markdown(
            "- **Certificación** = lo que se cobra al cliente (venta), por capítulo y partida, con origen, anterior y mes.\n"
            "- **Facturas** = lo que cuesta, leídas con IA o a mano, verificadas con 22 controles e imputadas a capítulo.\n"
            "- **Ofertas / contratos** = lo comprometido con cada industrial: quién ofertó, quién valoró, a quién se adjudicó.\n"
            "- **Rentabilidad** = venta − coste por capítulo, del mes o a origen, con alertas y precio de venta vs coste por partida.\n"
            "- **Asistente IA** = preguntas en lenguaje natural; las cifras siempre salen del sistema, no de la IA.")
