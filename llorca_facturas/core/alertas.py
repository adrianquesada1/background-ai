"""
Centro de alertas «Hoy»: lo que requiere acción, para cada persona según su cargo y sus obras.
Reúne en un sitio lo que está repartido por la aplicación, con prioridad y enlace a la pantalla donde se resuelve.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import db


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
    return sorted(out, key=lambda x: x["prioridad"])
