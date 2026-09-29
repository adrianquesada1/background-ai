"""
REMESAS DE PAGO a proveedores con fichero SEPA (pain.001.001.03, el estándar que admiten todos los bancos españoles).

Controles antifraude y de error antes de pagar:
- solo facturas APROBADAS y no pagadas;
- IBAN válido; si el IBAN del proveedor ha cambiado y nadie lo ha verificado (incidencia de cambio de cuenta abierta), NO se paga;
- una factura no puede ir en dos remesas vivas;
- importe a pagar = líquido de la factura (descontadas retenciones);
- la remesa se genera, se descarga, se sube al banco y SOLO cuando el banco confirma se marcan como pagadas.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from decimal import Decimal

from . import db
from .fiscal import validate_iban, normalize_iban

SCHEMA = """
CREATE TABLE IF NOT EXISTS remesas (
    id INTEGER PRIMARY KEY, referencia TEXT UNIQUE, fecha_ejecucion TEXT, estado TEXT DEFAULT 'generada',
    num_pagos INTEGER, total_cents INTEGER, creado_por TEXT, creado_en TEXT, confirmado_por TEXT, confirmado_en TEXT, anulado_por TEXT);
CREATE TABLE IF NOT EXISTS remesa_items (
    remesa_id INTEGER NOT NULL REFERENCES remesas(id) ON DELETE CASCADE, documento_id INTEGER NOT NULL,
    iban TEXT, importe_cents INTEGER, PRIMARY KEY (remesa_id, documento_id));
"""


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def candidatas(con, hasta: str | None = None, obra_id: int | None = None) -> list[dict]:
    q = """SELECT d.id, d.fecha, d.fecha_vencimiento, d.numero, d.obra_id, o.codigo AS obra, d.proveedor_id,
                  COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, d.emisor_nif, d.iban, d.total_a_pagar_cents
           FROM documentos d LEFT JOIN proveedores pr ON pr.id=d.proveedor_id LEFT JOIN obras o ON o.id=d.obra_id
           WHERE d.estado='aprobada' AND COALESCE(d.pagada,0)=0 AND d.tipo_documento IN ('factura','anticipo')
             AND COALESCE(d.total_a_pagar_cents,0) > 0
             AND d.id NOT IN (SELECT ri.documento_id FROM remesa_items ri JOIN remesas r ON r.id=ri.remesa_id WHERE r.estado='generada')"""
    p = []
    if hasta:
        q += " AND COALESCE(d.fecha_vencimiento, d.fecha) <= ?"; p.append(hasta)
    if obra_id:
        q += " AND d.obra_id=?"; p.append(obra_id)
    out = []
    for d in db.rows(con, q + " ORDER BY COALESCE(d.fecha_vencimiento, d.fecha)", p):
        motivos = []
        iban = normalize_iban(d["iban"]) if d["iban"] else None
        if not iban:
            motivos.append("sin IBAN")
        elif not validate_iban(iban)[0]:
            motivos.append("IBAN no válido")
        if db.one(con, "SELECT 1 x FROM incidencias WHERE documento_id=? AND codigo='C11' AND resuelta=0", (d["id"],)):
            motivos.append("cambio de cuenta bancaria sin verificar")
        if d["proveedor_id"] and iban and db.one(con, "SELECT 1 x FROM proveedor_ibans WHERE proveedor_id=? AND iban=? AND COALESCE(verificado,0)=0 "
                                                      "AND (SELECT COUNT(*) FROM proveedor_ibans WHERE proveedor_id=?)>1", (d["proveedor_id"], iban, d["proveedor_id"])):
            motivos.append("el proveedor tiene varias cuentas y esta no está verificada")
        out.append({**d, "iban_n": iban, "bloqueos": motivos})
    return out


def _txt(s: str, n: int) -> str:
    s = re.sub(r"[^A-Za-z0-9 /\-?:().,'+]", " ", str(s or "").replace("Ñ", "N").replace("ñ", "n")
               .translate(str.maketrans("ÁÉÍÓÚáéíóúÜüÇç", "AEIOUaeiouUuCc")))
    return html.escape(re.sub(r"\s+", " ", s).strip()[:n])


def generar(con, ids: list[int], fecha_ejecucion: str, usuario: str, rol: str) -> tuple[int, bytes]:
    if rol not in ("admin", "gestor", "direccion"):
        raise ValueError("Solo Administración o Dirección generan remesas.")
    ordenante = {"nombre": db.get_setting(con, "sepa_nombre", ""), "iban": normalize_iban(db.get_setting(con, "sepa_iban", "") or ""),
                 "bic": db.get_setting(con, "sepa_bic", ""), "id": db.get_setting(con, "sepa_id", "")}
    if not ordenante["nombre"] or not validate_iban(ordenante["iban"] or "")[0]:
        raise ValueError("Configure antes el ordenante (nombre e IBAN de la cuenta de pago) en Configuración → Pagos SEPA.")
    cands = {c["id"]: c for c in candidatas(con)}
    sel = [cands[i] for i in ids if i in cands]
    bloq = [f"{c['proveedor']} {c['numero']}: {', '.join(c['bloqueos'])}" for c in sel if c["bloqueos"]]
    if bloq:
        raise ValueError("No se pueden pagar: " + "; ".join(bloq))
    if not sel:
        raise ValueError("No hay facturas válidas seleccionadas.")
    total = sum(c["total_a_pagar_cents"] for c in sel)
    ref = f"REM{datetime.now():%Y%m%d%H%M%S}"
    ahora = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    E = lambda c: f"{Decimal(c) / 100:.2f}"  # noqa: E731
    pagos = "".join(f"""
      <CdtTrfTxInf><PmtId><EndToEndId>{_txt(f"{ref}-{c['id']}", 35)}</EndToEndId></PmtId>
        <Amt><InstdAmt Ccy="EUR">{E(c['total_a_pagar_cents'])}</InstdAmt></Amt>
        <Cdtr><Nm>{_txt(c['proveedor'], 70)}</Nm></Cdtr>
        <CdtrAcct><Id><IBAN>{c['iban_n']}</IBAN></Id></CdtrAcct>
        <RmtInf><Ustrd>{_txt(f"Fra {c['numero']} obra {c['obra'] or ''}", 140)}</Ustrd></RmtInf>
      </CdtTrfTxInf>""" for c in sel)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <CstmrCdtTrfInitn>
    <GrpHdr><MsgId>{ref}</MsgId><CreDtTm>{ahora}</CreDtTm><NbOfTxs>{len(sel)}</NbOfTxs><CtrlSum>{E(total)}</CtrlSum>
      <InitgPty><Nm>{_txt(ordenante['nombre'], 70)}</Nm>{f"<Id><OrgId><Othr><Id>{_txt(ordenante['id'], 35)}</Id></Othr></OrgId></Id>" if ordenante['id'] else ''}</InitgPty></GrpHdr>
    <PmtInf><PmtInfId>{ref}-1</PmtInfId><PmtMtd>TRF</PmtMtd><NbOfTxs>{len(sel)}</NbOfTxs><CtrlSum>{E(total)}</CtrlSum>
      <PmtTpInf><SvcLvl><Cd>SEPA</Cd></SvcLvl></PmtTpInf><ReqdExctnDt>{fecha_ejecucion}</ReqdExctnDt>
      <Dbtr><Nm>{_txt(ordenante['nombre'], 70)}</Nm></Dbtr><DbtrAcct><Id><IBAN>{ordenante['iban']}</IBAN></Id></DbtrAcct>
      <DbtrAgt><FinInstnId>{f"<BIC>{_txt(ordenante['bic'], 11)}</BIC>" if ordenante['bic'] else "<Othr><Id>NOTPROVIDED</Id></Othr>"}</FinInstnId></DbtrAgt>
      <ChrgBr>SLEV</ChrgBr>{pagos}
    </PmtInf>
  </CstmrCdtTrfInitn>
</Document>
"""
    with db.tx(con):
        cur = con.execute("INSERT INTO remesas (referencia, fecha_ejecucion, estado, num_pagos, total_cents, creado_por, creado_en) VALUES (?,?,?,?,?,?,?)",
                          (ref, fecha_ejecucion, "generada", len(sel), total, usuario, db.now_iso()))
        rid = cur.lastrowid
        for c in sel:
            con.execute("INSERT INTO remesa_items (remesa_id, documento_id, iban, importe_cents) VALUES (?,?,?,?)",
                        (rid, c["id"], c["iban_n"], c["total_a_pagar_cents"]))
        db.audit(con, usuario, "generar_remesa", "remesa", rid, {"referencia": ref, "pagos": len(sel), "total": E(total)})
    return rid, xml.encode("utf-8")


def confirmar(con, rid: int, fecha_pago: str, usuario: str) -> int:
    items = db.rows(con, "SELECT documento_id FROM remesa_items WHERE remesa_id=?", (rid,))
    with db.tx(con):
        for it in items:
            con.execute("UPDATE documentos SET pagada=1, fecha_pago=? WHERE id=?", (fecha_pago, it["documento_id"]))
        con.execute("UPDATE remesas SET estado='pagada', confirmado_por=?, confirmado_en=? WHERE id=?", (usuario, db.now_iso(), rid))
        db.audit(con, usuario, "confirmar_remesa", "remesa", rid, {"facturas": len(items), "fecha_pago": fecha_pago})
    return len(items)


def anular(con, rid: int, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE remesas SET estado='anulada', anulado_por=? WHERE id=? AND estado='generada'", (usuario, rid))
        db.audit(con, usuario, "anular_remesa", "remesa", rid, None)
