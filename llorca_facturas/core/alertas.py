"""
Centro de alertas «Hoy»: lo que requiere acción, para cada persona según su cargo y sus obras.
Reúne en un sitio lo que está repartido por la aplicación, con prioridad y enlace a la pantalla donde se resuelve.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from . import db

_CACHE: dict = {}


def _q(con, sql, p=()):
    try:
        return db.one(con, sql, p) or {}
    except Exception:
        return {}


def calcular(con, rol: str, obras: set | None) -> list[dict]:
    hoy = date.today()
    f_obra, par = "", []
    if obras is not None:
        if not obras:
            return []
        f_obra = f" AND obra_id IN ({','.join('?' * len(obras))})"
        par = list(obras)
    out = []

    def add(prio, area, texto, n, pagina):
        if n:
            out.append({"prioridad": prio, "area": area, "texto": texto.format(n=n), "n": n, "pagina": pagina})
    admin = rol in ("admin", "gestor", "direccion")
    # ---- facturas
    add(1, "Facturas", "{n} incidencia(s) CRÍTICA(S) abiertas (duplicados, cambios de IBAN, albaranes repetidos…)",
        _q(con, f"""SELECT COUNT(*) n FROM incidencias i JOIN documentos d ON d.id=i.documento_id WHERE i.resuelta=0 AND i.severidad='critica'
                    AND d.estado NOT IN ('rechazada','eliminado','duplicado'){f_obra.replace('obra_id', 'd.obra_id')}""", par).get("n"), "incidencias")
    if rol in ("jefe_obra", "admin", "direccion"):
        q = """SELECT COUNT(*) n FROM documentos d WHERE d.conformado_por IS NULL AND d.estado IN ('pendiente_revision','revisada')
               AND d.obra_id IN (SELECT obra_id FROM obra_usuarios ou JOIN usuarios u ON u.id=ou.usuario_id WHERE u.rol='jefe_obra')"""
        add(2, "Facturas", "{n} factura(s) esperan la conformidad de obra", _q(con, q + f_obra.replace("obra_id", "d.obra_id"), par).get("n"), "pendientes")
    dias = int(db.get_setting(con, "dias_aviso_aprobacion", "15") or 15)
    add(2, "Facturas", f"{{n}} factura(s) llevan más de {dias} días sin aprobar",
        _q(con, f"""SELECT COUNT(*) n FROM documentos WHERE estado IN ('pendiente_revision','revisada') AND creado_en < ?{f_obra}""",
           [(hoy - timedelta(days=dias)).isoformat()] + par).get("n"), "pendientes")
    if admin:
        add(3, "Facturas", "{n} documento(s) subidos aún sin leer", _q(con, "SELECT COUNT(*) n FROM documentos WHERE estado='sin_procesar'").get("n"), "ingesta")
        # ---- pagos
        add(1, "Pagos", "{n} factura(s) aprobadas VENCIDAS sin pagar",
            _q(con, """SELECT COUNT(*) n FROM documentos WHERE estado='aprobada' AND COALESCE(pagada,0)=0 AND tipo_documento IN ('factura','anticipo')
                       AND COALESCE(fecha_vencimiento, date(fecha,'+60 day')) < ?""", (hoy.isoformat(),)).get("n"), "pagos")
        add(2, "Pagos", "{n} factura(s) vencen en los próximos 7 días",
            _q(con, """SELECT COUNT(*) n FROM documentos WHERE estado='aprobada' AND COALESCE(pagada,0)=0 AND tipo_documento IN ('factura','anticipo')
                       AND COALESCE(fecha_vencimiento, date(fecha,'+60 day')) BETWEEN ? AND ?""", (hoy.isoformat(), (hoy + timedelta(days=7)).isoformat())).get("n"), "pagos")
        add(2, "Pagos", "{n} remesa(s) generadas pendientes de confirmar con el banco",
            _q(con, "SELECT COUNT(*) n FROM remesas WHERE estado='generada'").get("n"), "pagos")
        # ---- cobros y garantías
        add(1, "Cobros", "{n} factura(s) emitidas vencidas sin cobrar",
            _q(con, """SELECT COUNT(*) n FROM facturas_emitidas f WHERE estado='emitida' AND vencimiento < ? AND liquido_cents >
                       COALESCE((SELECT SUM(importe_cents) FROM cobros c WHERE c.factura_id=f.id AND c.concepto='factura'),0)""", (hoy.isoformat(),)).get("n"), "tesoreria")
        add(2, "Cobros", "{n} retención(es) de garantía del cliente vencidas o a punto: reclamar",
            _q(con, """SELECT COUNT(*) n FROM facturas_emitidas f WHERE estado='emitida' AND retencion_cents>0 AND retencion_vence <= ?
                       AND retencion_cents > COALESCE((SELECT SUM(importe_cents) FROM cobros c WHERE c.factura_id=f.id AND c.concepto='retencion'),0)""",
               ((hoy + timedelta(days=int(db.get_setting(con, "aviso_retencion_dias", "30")))).isoformat(),)).get("n"), "tesoreria")
        add(2, "Cobros", "{n} certificación(es) sin factura al cliente",
            _q(con, """SELECT COUNT(*) n FROM certificaciones c WHERE c.total_actual_m<>0 AND NOT EXISTS
                       (SELECT 1 FROM facturas_emitidas f WHERE f.cert_id=c.id AND f.estado<>'anulada')""").get("n"), "tesoreria")
        add(2, "Garantías", "{n} aval(es) vencidos que siguen vivos (comisión corriendo)",
            _q(con, "SELECT COUNT(*) n FROM avales WHERE estado='vivo' AND fecha_vencimiento < ?", (hoy.isoformat(),)).get("n"), "avales")
        add(3, "Internos", "{n} coste(s)/venta(s) interna(s) pendientes de validar",
            _q(con, "SELECT COUNT(*) n FROM mov_internos WHERE estado='borrador'").get("n"), "internos")
    # ---- obra
    add(2, "Obra", "{n} compromiso(s) de reunión vencidos",
        _q(con, f"SELECT COUNT(*) n FROM obra_compromisos WHERE estado='abierto' AND fecha_limite < ?{f_obra}", [hoy.isoformat()] + par).get("n"), "gobierno")
    add(3, "Obra", "{n} posible(s) extra(s) sin valorar",
        _q(con, f"SELECT COUNT(*) n FROM obra_compromisos WHERE tipo='extra' AND estado='abierto'{f_obra}", par).get("n"), "gobierno")
    if admin:
        mes_ant = (hoy.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        add(2, "Cierre", f"{{n}} obra(s) activa(s) sin cerrar el mes {mes_ant}",
            _q(con, """SELECT COUNT(*) n FROM obras o WHERE o.activa=1 AND EXISTS (SELECT 1 FROM certificaciones c WHERE c.obra_id=o.id)
                       AND NOT EXISTS (SELECT 1 FROM cierres k WHERE k.obra_id=o.id AND k.periodo=? AND k.estado='cerrado')""", (mes_ant,)).get("n"), "cierre")
    _alertas_v2(con, rol, obras, f_obra, par, add, admin)
    return sorted(out, key=lambda x: x["prioridad"])


def _n(filas) -> int:
    return len(filas) if filas else 0


def _alertas_v2(con, rol, obras, f_obra, par, add, admin):
    """Buzón, copias, correo, SIS, conciliación, subcontratas, planning, planos, CAE y residuos."""
    def seguro(f, *a):
        try:
            return f(*a)
        except Exception:  # noqa: BLE001 - una alerta que falla nunca rompe el centro de alertas
            return None
    if rol == "admin":
        from . import respaldo
        for a in seguro(respaldo.avisos, con) or []:
            out_prio = 1 if ("ninguna" in a or "más de un día" in a or "DESACTIVADA" in a or "falló" in a) else 2
            add(out_prio, "Copias", a.replace("{", "{{").replace("}", "}}"), 1, "automatizaciones")
        err = _q(con, """SELECT COUNT(*) n FROM tareas_programadas WHERE ultimo_error IS NOT NULL AND ultimo_error<>''""").get("n")
        add(2, "Automatizaciones", "{n} tarea(s) automática(s) con error en su última ejecución", err, "automatizaciones")
    if admin:
        add(2, "Facturas", "{n} adjunto(s) del buzón dudosos: decidir si son facturas",
            _q(con, "SELECT COUNT(*) n FROM buzon_adjuntos WHERE clasificacion='dudoso' AND documento_id IS NULL").get("n"), "buzon")
        add(2, "Facturas", "{n} correo(s) del buzón con error al procesarse",
            _q(con, "SELECT COUNT(*) n FROM buzon_mensajes WHERE estado='error'").get("n"), "buzon")
        add(2, "Correo", "{n} correo(s) preparados esperan aprobación para enviarse",
            _q(con, "SELECT COUNT(*) n FROM correo_salida WHERE estado='pendiente_aprobacion'").get("n"), "correo")
        add(1, "Correo", "{n} correo(s) no se han podido enviar",
            _q(con, "SELECT COUNT(*) n FROM correo_salida WHERE estado='fallido'").get("n"), "correo")
        add(1, "SIS", "{n} factura(s) cambiaron después de contabilizarse en SIS: ajustar el asiento",
            _q(con, "SELECT COUNT(*) n FROM sis_envios WHERE estado='desfasado'").get("n"), "sis")
        add(2, "SIS", "{n} asiento(s) no se han podido enviar a SIS",
            _q(con, "SELECT COUNT(*) n FROM sis_envios WHERE estado='fallido'").get("n"), "sis")
        add(3, "Banco", "{n} movimiento(s) bancarios sin conciliar de hace más de 7 días",
            _q(con, "SELECT COUNT(*) n FROM banco_movimientos WHERE estado='pendiente' AND fecha < ?",
               ((date.today() - timedelta(days=7)).isoformat(),)).get("n"), "conciliacion")
        add(2, "Subcontratas", "{n} certificación(es) de subcontrata medidas pendientes de aprobar",
            _q(con, f"SELECT COUNT(*) n FROM certs_proveedor WHERE estado='borrador' AND medido_en IS NOT NULL AND base_mes_cents<>0{f_obra}", par).get("n"),
            "cert_proveedor")
    if rol in ("admin", "direccion", "gestor", "jefe_obra", "tecnico"):
        clave = (rol, tuple(sorted(obras)) if obras is not None else None)
        guardado = _CACHE.get(clave)
        if guardado and time.time() - guardado[0] < 120:          # el centro de alertas se refresca cada pocos segundos
            for args in guardado[1]:
                add(*args)
            return
        filas = []

        def add(*args, _add=add):  # noqa: F811 - se registra también para la caché
            filas.append(args)
            _add(*args)
        from . import planificacion, planos, prevencion
        ids = [r["id"] for r in db.rows(con, "SELECT id FROM obras WHERE activa=1")] if obras is None else list(obras)
        n_ret = sum(_n(seguro(planificacion.retrasadas, con, o)) for o in ids
                    if _q(con, "SELECT 1 x FROM plan_actividades WHERE obra_id=? LIMIT 1", (o,)))
        add(2, "Planning", "{n} actividad(es) del planning retrasadas sobre lo previsto (actualizar y replanificar)", n_ret, "planificacion")
        add(2, "Planos", "{n} revisión(es) de planos sin respuesta de la dirección facultativa",
            sum(_n(seguro(planos.pendientes_df, con, o)) for o in ids), "planos")
        add(2, "Planos", "{n} copia(s) entregadas de planos superados sin retirar", sum(_n(seguro(planos.entregas_a_retirar, con, o)) for o in ids), "planos")
        add(1, "Seguridad", "{n} empresa(s) en obra con documentación CAE obligatoria que falta o ha caducado",
            _n(seguro(prevencion.empresas_bloqueadas, con)), "prevencion")
        add(3, "Seguridad", "{n} documento(s) CAE caducan en los próximos 15 días", _n(seguro(prevencion.caducan_pronto, con, 15)), "prevencion")
        add(2, "Residuos", "{n} retirada(s) de residuos sin certificado del gestor", _n(seguro(prevencion.sin_certificado, con)), "prevencion")
        _CACHE[clave] = (time.time(), filas)
