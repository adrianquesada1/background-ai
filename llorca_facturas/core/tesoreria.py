"""
TESORERÍA: recuperar la caja que está fuera.

- Factura emitida a partir de la certificación firmada (importe del mes), con la serie SII que corresponda y numeración
  correlativa por serie. Queda en borrador hasta que Administración la emite (el sistema propone, la persona aprueba).
- Cobros (totales o parciales) y aging de lo pendiente por cliente y obra.
- Radar de retenciones de garantía que el CLIENTE retiene a Llorca (cuentas 430/4308): vencimiento (365 días por defecto),
  aviso previo y borrador de reclamación listo para enviar. Se pueden dar de alta las retenciones históricas.
- Seguimiento de cada cobro: quién reclamó, cuándo, cómo y qué respondió el cliente.
- Previsión de caja semanal: cobros esperados frente a pagos a proveedores por vencimiento.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from decimal import Decimal

from . import db
from .money import fmt_eur, q2, to_cents

SCHEMA = """
CREATE TABLE IF NOT EXISTS facturas_emitidas (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER REFERENCES obras(id) ON DELETE SET NULL,
    cert_id INTEGER REFERENCES certificaciones(id) ON DELETE SET NULL,
    serie TEXT NOT NULL, numero INTEGER, codigo TEXT, fecha TEXT, vencimiento TEXT,
    sociedad_nif TEXT, cliente TEXT, cliente_nif TEXT, concepto TEXT,
    base_cents INTEGER NOT NULL, iva_pct TEXT, cuota_cents INTEGER, isp INTEGER DEFAULT 0,
    retencion_pct TEXT, retencion_cents INTEGER DEFAULT 0, total_cents INTEGER, liquido_cents INTEGER,
    retencion_vence TEXT, retencion_estado TEXT DEFAULT 'retenida',
    estado TEXT DEFAULT 'borrador', historica INTEGER DEFAULT 0,
    creado_por TEXT, creado_en TEXT, emitida_por TEXT, emitida_en TEXT, notas TEXT,
    UNIQUE (serie, numero)
);
CREATE TABLE IF NOT EXISTS cobros (
    id INTEGER PRIMARY KEY, factura_id INTEGER NOT NULL REFERENCES facturas_emitidas(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL, importe_cents INTEGER NOT NULL, concepto TEXT DEFAULT 'factura', medio TEXT, notas TEXT,
    usuario TEXT, creado_en TEXT
);
CREATE TABLE IF NOT EXISTS seguimiento_cobros (
    id INTEGER PRIMARY KEY, factura_id INTEGER REFERENCES facturas_emitidas(id) ON DELETE CASCADE,
    obra_id INTEGER, fecha TEXT, usuario TEXT, accion TEXT, contacto TEXT, respuesta TEXT, proxima_fecha TEXT, sobre TEXT DEFAULT 'factura'
);
"""
ACCIONES = ["Recordatorio por correo", "Llamada", "Reunión", "Reclamación formal", "Reclamación de retención", "Otro"]


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def config(con) -> dict:
    return {"series": [s.strip() for s in db.get_setting(con, "series_sii", "F1,R1").split(",") if s.strip()],
            "iva_pct": Decimal(db.get_setting(con, "iva_venta_defecto", "10")),
            "ret_pct": Decimal(db.get_setting(con, "retencion_cliente_defecto", "5")),
            "dias_venc": int(db.get_setting(con, "dias_vencimiento_venta", "60")),
            "dias_ret": int(db.get_setting(con, "dias_retencion_cliente", "365")),
            "aviso_ret": int(db.get_setting(con, "aviso_retencion_dias", "30"))}


def siguiente_numero(con, serie: str, anio: int) -> int:
    r = db.one(con, "SELECT MAX(numero) n FROM facturas_emitidas WHERE serie=? AND substr(fecha,1,4)=?", (serie, str(anio)))
    return ((r and r["n"]) or 0) + 1


def desde_certificacion(con, cert_id: int, serie: str, iva_pct, ret_pct, fecha: str, usuario: str, isp: bool = False) -> int:
    """Borrador de factura de venta con el importe del mes de la certificación. No se duplica: una por certificación."""
    ya = db.one(con, "SELECT id, codigo FROM facturas_emitidas WHERE cert_id=? AND estado<>'anulada'", (cert_id,))
    if ya:
        raise ValueError(f"Esta certificación ya tiene factura ({ya['codigo'] or 'borrador #' + str(ya['id'])}).")
    c = db.one(con, "SELECT c.*, o.cliente AS cli_obra, o.codigo AS obra_cod, o.nombre AS obra_nombre FROM certificaciones c "
                    "JOIN obras o ON o.id=c.obra_id WHERE c.id=?",
               (cert_id,))
    base = q2(Decimal(c["total_actual_m"]) / 1000)
    if base == 0:
        raise ValueError("La certificación no tiene importe en el mes.")
    iva_pct, ret_pct = Decimal(str(iva_pct)), Decimal(str(ret_pct))
    cuota = Decimal(0) if isp else q2(base * iva_pct / 100)
    ret = q2(base * ret_pct / 100)
    total = base + cuota
    cfg = config(con)
    f = date.fromisoformat(fecha)
    from .validation import sociedades
    soc = next(iter(sociedades(con)), None)
    with db.tx(con):
        cur = con.execute("""INSERT INTO facturas_emitidas (obra_id, cert_id, serie, fecha, vencimiento, sociedad_nif, cliente, concepto,
                             base_cents, iva_pct, cuota_cents, isp, retencion_pct, retencion_cents, total_cents, liquido_cents,
                             retencion_vence, estado, creado_por, creado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (c["obra_id"], cert_id, serie, f.isoformat(), (f + timedelta(days=cfg["dias_venc"])).isoformat(), soc,
                           c.get("cliente") or c["cli_obra"], f"Certificación nº {c['numero']} de la obra {c['obra_cod']} "
                                                          f"({c.get('obra_nombre') or ''}), periodo hasta {c['fecha']}",
                           to_cents(base), str(iva_pct), to_cents(cuota), int(isp), str(ret_pct), to_cents(ret), to_cents(total),
                           to_cents(total - ret), (f + timedelta(days=cfg["dias_ret"])).isoformat(), "borrador", usuario, db.now_iso()))
        db.audit(con, usuario, "borrador_factura_emitida", "factura_emitida", cur.lastrowid, {"cert": cert_id, "base": str(base)})
    return cur.lastrowid


def emitir(con, fid: int, usuario: str, rol: str) -> str:
    if rol not in ("admin", "gestor", "direccion"):
        raise ValueError("Solo Administración o Dirección emiten facturas.")
    f = db.one(con, "SELECT * FROM facturas_emitidas WHERE id=?", (fid,))
    if f["estado"] != "borrador":
        raise ValueError("Solo se emiten borradores.")
    if not f["cliente_nif"]:
        raise ValueError("Falta el NIF del cliente.")
    from .fiscal import validate_nif, normalize_nif
    if not validate_nif(normalize_nif(f["cliente_nif"]))[0]:
        raise ValueError("El NIF del cliente no es válido.")
    n = siguiente_numero(con, f["serie"], int(f["fecha"][:4]))
    codigo = f"{f['serie']}-{f['fecha'][:4]}/{n:04d}"
    with db.tx(con):
        con.execute("UPDATE facturas_emitidas SET numero=?, codigo=?, estado='emitida', emitida_por=?, emitida_en=? WHERE id=?",
                    (n, codigo, usuario, db.now_iso(), fid))
        db.audit(con, usuario, "emitir_factura", "factura_emitida", fid, {"codigo": codigo})
    return codigo


def alta_historica(con, obra_id: int, cliente: str, cliente_nif: str, codigo: str, fecha: str, base, iva_pct, ret_pct, cobrado,
                   usuario: str, retencion_vence: str | None = None) -> int:
    """Facturas emitidas antiguas (para el aging y el inventario de retenciones de las cuentas 430/4308)."""
    base = q2(Decimal(str(base)))
    cuota = q2(base * Decimal(str(iva_pct)) / 100)
    ret = q2(base * Decimal(str(ret_pct)) / 100)
    total = base + cuota
    cfg = config(con)
    f = date.fromisoformat(fecha)
    with db.tx(con):
        cur = con.execute("""INSERT INTO facturas_emitidas (obra_id, serie, codigo, fecha, vencimiento, cliente, cliente_nif, concepto, base_cents,
                             iva_pct, cuota_cents, retencion_pct, retencion_cents, total_cents, liquido_cents, retencion_vence, estado,
                             historica, creado_por, creado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                          (obra_id, "HIST", codigo, f.isoformat(), (f + timedelta(days=cfg["dias_venc"])).isoformat(), cliente, cliente_nif,
                           "Alta histórica", to_cents(base), str(iva_pct), to_cents(cuota), str(ret_pct), to_cents(ret), to_cents(total),
                           to_cents(total - ret), retencion_vence or (f + timedelta(days=cfg["dias_ret"])).isoformat(), "emitida",
                           usuario, db.now_iso()))
        fid = cur.lastrowid
        if cobrado and Decimal(str(cobrado)) > 0:
            con.execute("INSERT INTO cobros (factura_id, fecha, importe_cents, concepto, medio, usuario, creado_en) VALUES (?,?,?,?,?,?,?)",
                        (fid, f.isoformat(), to_cents(Decimal(str(cobrado))), "factura", "histórico", usuario, db.now_iso()))
        db.audit(con, usuario, "alta_factura_emitida_historica", "factura_emitida", fid, {"codigo": codigo})
    return fid


def cobrar(con, fid: int, fecha: str, importe, concepto: str, medio: str, usuario: str, notas: str = "") -> None:
    imp = q2(Decimal(str(importe)))
    if imp <= 0:
        raise ValueError("El importe cobrado debe ser positivo.")
    f = estado_factura(con, fid)
    pendiente = f["pendiente_ret_c"] if concepto == "retencion" else f["pendiente_c"]
    if to_cents(imp) > pendiente + 1:
        raise ValueError(f"El cobro ({fmt_eur(imp)}) supera lo pendiente ({fmt_eur(Decimal(pendiente) / 100)}).")
    with db.tx(con):
        con.execute("INSERT INTO cobros (factura_id, fecha, importe_cents, concepto, medio, notas, usuario, creado_en) VALUES (?,?,?,?,?,?,?,?)",
                    (fid, fecha, to_cents(imp), concepto, medio, notas, usuario, db.now_iso()))
        if concepto == "retencion" and to_cents(imp) >= pendiente:
            con.execute("UPDATE facturas_emitidas SET retencion_estado='cobrada' WHERE id=?", (fid,))
        db.audit(con, usuario, "registrar_cobro", "factura_emitida", fid, {"importe": str(imp), "concepto": concepto})


def estado_factura(con, fid: int) -> dict:
    f = db.one(con, "SELECT * FROM facturas_emitidas WHERE id=?", (fid,))
    cob = db.one(con, "SELECT COALESCE(SUM(CASE WHEN concepto='factura' THEN importe_cents END),0) f, "
                      "COALESCE(SUM(CASE WHEN concepto='retencion' THEN importe_cents END),0) r FROM cobros WHERE factura_id=?", (fid,))
    return {**f, "cobrado_c": cob["f"], "cobrado_ret_c": cob["r"], "pendiente_c": (f["liquido_cents"] or 0) - cob["f"],
            "pendiente_ret_c": (f["retencion_cents"] or 0) - cob["r"]}


def listado(con, obra_id: int | None = None, incluir_borradores: bool = True) -> list[dict]:
    q = "SELECT f.id FROM facturas_emitidas f WHERE f.estado<>'anulada'"
    p = []
    if obra_id:
        q += " AND f.obra_id=?"; p.append(obra_id)
    if not incluir_borradores:
        q += " AND f.estado<>'borrador'"
    return [estado_factura(con, r["id"]) for r in db.rows(con, q + " ORDER BY f.fecha DESC", p)]


def aging(con, obra_id: int | None = None) -> list[dict]:
    hoy = date.today()
    out = []
    for f in listado(con, obra_id, False):
        if f["pendiente_c"] <= 0:
            continue
        dias = (hoy - date.fromisoformat(f["vencimiento"])).days if f["vencimiento"] else 0
        tramo = "No vencido" if dias <= 0 else "1-30 días" if dias <= 30 else "31-60 días" if dias <= 60 else "61-90 días" if dias <= 90 else "Más de 90 días"
        out.append({**f, "dias_vencido": max(0, dias), "tramo": tramo})
    return out


def radar_retenciones(con, obra_id: int | None = None) -> list[dict]:
    hoy = date.today()
    aviso = config(con)["aviso_ret"]
    out = []
    for f in listado(con, obra_id, False):
        if f["pendiente_ret_c"] <= 0:
            continue
        vence = date.fromisoformat(f["retencion_vence"]) if f["retencion_vence"] else None
        dias = (vence - hoy).days if vence else None
        estado = "Vencida: reclamar" if dias is not None and dias < 0 else "Vence pronto" if dias is not None and dias <= aviso else "Retenida"
        ult = db.one(con, "SELECT fecha, usuario, respuesta FROM seguimiento_cobros WHERE factura_id=? AND sobre='retencion' ORDER BY id DESC LIMIT 1",
                     (f["id"],))
        out.append({**f, "dias_para_vencer": dias, "situacion": estado, "ultima_reclamacion": ult})
    return sorted(out, key=lambda x: (x["dias_para_vencer"] if x["dias_para_vencer"] is not None else 99999))


def borrador_reclamacion(con, fid: int, firmante: str) -> str:
    f = estado_factura(con, fid)
    o = db.one(con, "SELECT codigo, nombre FROM obras WHERE id=?", (f["obra_id"],)) or {"codigo": "", "nombre": ""}
    return (f"Asunto: Devolución de retención de garantía · obra {o['codigo']} {o['nombre']} · factura {f['codigo']}\n\n"
            f"Estimados señores de {f['cliente'] or ''}:\n\n"
            f"Les recordamos que el {f['retencion_vence']} venció el plazo de garantía de la retención practicada sobre nuestra "
            f"factura {f['codigo']} de fecha {f['fecha']}, por importe de {fmt_eur(Decimal(f['pendiente_ret_c']) / 100)}, "
            f"correspondiente a la obra {o['nombre']}.\n\n"
            "Les rogamos que procedan a su devolución mediante transferencia a la cuenta habitual o que nos indiquen, en su caso, "
            "cualquier incidencia pendiente que la impida.\n\nQuedamos a su disposición.\n\nAtentamente,\n\n"
            f"{firmante}\nLlorca Group\n")


def seguimiento(con, fid: int | None, obra_id: int | None, accion: str, contacto: str, respuesta: str, proxima: str | None,
                usuario: str, sobre: str = "factura") -> None:
    with db.tx(con):
        con.execute("INSERT INTO seguimiento_cobros (factura_id, obra_id, fecha, usuario, accion, contacto, respuesta, proxima_fecha, sobre) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (fid, obra_id, date.today().isoformat(), usuario, accion, contacto, respuesta, proxima, sobre))
        db.audit(con, usuario, "seguimiento_cobro", "factura_emitida", fid, {"accion": accion, "sobre": sobre})


def prevision_caja(con, semanas: int = 12, saldo_inicial: Decimal = Decimal(0)) -> list[dict]:
    """Semana a semana: cobros pendientes por vencimiento frente a pagos a proveedores (aprobadas sin pagar) por vencimiento.
    Lo ya vencido y pendiente se sitúa en la primera semana."""
    hoy = date.today()
    inicio = hoy - timedelta(days=hoy.weekday())
    semanas_l = [inicio + timedelta(weeks=i) for i in range(semanas)]
    cob = {s: Decimal(0) for s in semanas_l}
    pag = {s: Decimal(0) for s in semanas_l}

    def cubo(fecha_txt):
        f = date.fromisoformat(fecha_txt) if fecha_txt else hoy
        if f < inicio:
            return semanas_l[0]
        k = (f - inicio).days // 7
        return semanas_l[k] if k < semanas else None
    for f in listado(con, None, False):
        if f["pendiente_c"] > 0:
            s = cubo(f["vencimiento"])
            if s:
                cob[s] += Decimal(f["pendiente_c"]) / 100
    for d in db.rows(con, """SELECT COALESCE(fecha_vencimiento, date(fecha, '+60 day')) v, total_a_pagar_cents c FROM documentos
                             WHERE estado='aprobada' AND COALESCE(pagada,0)=0 AND tipo_documento IN ('factura','abono','anticipo')"""):
        s = cubo(d["v"])
        if s:
            pag[s] += Decimal(d["c"] or 0) / 100
    saldo, out = Decimal(saldo_inicial), []
    for s in semanas_l:
        saldo += cob[s] - pag[s]
        out.append({"semana": s.isoformat(), "cobros": cob[s], "pagos": pag[s], "neto": cob[s] - pag[s], "saldo": saldo})
    return out


def indicadores(con) -> dict:
    cobradas = db.rows(con, """SELECT f.fecha, MAX(c.fecha) ultimo FROM facturas_emitidas f JOIN cobros c ON c.factura_id=f.id
                              WHERE c.concepto='factura' AND f.historica=0 GROUP BY f.id""")
    demoras = [(date.fromisoformat(r["ultimo"]) - date.fromisoformat(r["fecha"])).days for r in cobradas if r["ultimo"] and r["fecha"]]
    recl = db.one(con, """SELECT COALESCE(SUM(f.retencion_cents),0) s FROM facturas_emitidas f WHERE EXISTS
                          (SELECT 1 FROM seguimiento_cobros s WHERE s.factura_id=f.id AND s.sobre='retencion')""")["s"]
    try:
        av = db.one(con, "SELECT COUNT(*) n, SUM(fecha_vencimiento IS NOT NULL) v FROM avales WHERE estado='vivo'")
    except Exception:
        av = {"n": 0, "v": 0}
    return {"demora_media": (sum(demoras) / len(demoras)) if demoras else None, "retenciones_reclamadas": Decimal(recl or 0) / 100,
            "pct_avales_vigilados": (100 * (av["v"] or 0) / av["n"]) if av and av["n"] else None}


def factura_html(con, fid: int) -> str:
    f = estado_factura(con, fid)
    o = db.one(con, "SELECT codigo, nombre FROM obras WHERE id=?", (f["obra_id"],)) or {"codigo": "", "nombre": ""}
    from .validation import sociedades
    soc = sociedades(con).get(f["sociedad_nif"] or "", "LLORCA GROUP HISPANIA, S.L.")
    E = lambda c: fmt_eur(Decimal(c or 0) / 100)  # noqa: E731
    borrador = f["estado"] == "borrador"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Factura {html.escape(f['codigo'] or 'borrador')}</title>
<style>body{{font-family:'Segoe UI',Arial,sans-serif;color:#2E2E2E;margin:18mm;font-size:11pt}} h1{{border-left:6px solid #E1251B;padding-left:10px}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:6px;border-bottom:1px solid #ddd;text-align:left}} .n{{text-align:right}}
.marca{{color:#E1251B;font-weight:700;font-size:14pt}}</style></head><body>
{'<p class="marca">BORRADOR · SIN VALIDEZ FISCAL HASTA SU EMISIÓN</p>' if borrador else ''}
<h1>FACTURA {html.escape(f['codigo'] or '')}</h1>
<table><tr><td><b>Emisor</b><br>{html.escape(soc)}<br>NIF {html.escape(f['sociedad_nif'] or '')}</td>
<td><b>Cliente</b><br>{html.escape(f['cliente'] or '')}<br>NIF {html.escape(f['cliente_nif'] or '—')}</td>
<td><b>Fecha</b> {f['fecha']}<br><b>Vencimiento</b> {f['vencimiento']}<br><b>Serie</b> {html.escape(f['serie'])}</td></tr></table>
<p><b>Obra:</b> {html.escape(o['codigo'])} · {html.escape(o['nombre'])}<br><b>Concepto:</b> {html.escape(f['concepto'] or '')}</p>
<table><tr><th>Concepto</th><th class="n">Importe</th></tr>
<tr><td>Base imponible</td><td class="n">{E(f['base_cents'])}</td></tr>
<tr><td>{'IVA: inversión del sujeto pasivo' if f['isp'] else 'IVA ' + str(f['iva_pct']) + ' %'}</td><td class="n">{E(f['cuota_cents'])}</td></tr>
<tr><td><b>Total factura</b></td><td class="n"><b>{E(f['total_cents'])}</b></td></tr>
<tr><td>Retención de garantía {f['retencion_pct']} %</td><td class="n">−{E(f['retencion_cents'])}</td></tr>
<tr><td><b>Líquido a percibir</b></td><td class="n"><b>{E(f['liquido_cents'])}</b></td></tr></table>
</body></html>"""
