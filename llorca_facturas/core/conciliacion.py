"""
CONCILIACIÓN BANCARIA: cruzar los movimientos del banco con cobros y pagos.

- Importa el extracto en NORMA 43 (cuaderno 43 de la AEB/CSB, el que descargan todos los bancos españoles) o en Excel/CSV
  (fecha, concepto e importe, o cargo/abono). El Norma 43 se comprueba con su propia aritmética: saldo inicial + abonos −
  cargos = saldo final, y los totales del registro 33.
- Un movimiento nunca entra dos veces aunque se importen extractos solapados (huella del movimiento).
- Para cada movimiento pendiente se proponen casaciones con su grado de confianza:
  * cargo = total de una remesa SEPA generada (± días de la fecha de ejecución) → confirma la remesa y marca pagadas sus
    facturas (hasta ahora se confirmaba a mano);
  * cargo = transferencia suelta de una factura de proveedor aprobada (importe líquido y nombre del proveedor en el concepto);
  * cargos individuales de cada pago de una remesa (bancos que los detallan): cuando están todos, se confirma la remesa;
  * abono = líquido pendiente de una factura emitida (o su retención de garantía), mejor si el concepto cita el número
    de factura o el cliente → registra el cobro.
- Lo que casa sin ambigüedad (una sola candidata y confianza alta) se puede aplicar solo; el resto espera a que una
  persona elija. Los movimientos sin contrapartida (comisiones, nóminas…) se marcan como «otros» con su motivo.
- Todo es reversible: deshacer una conciliación anula el cobro registrado o vuelve a dejar pendiente la remesa.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from datetime import date
from decimal import Decimal

from . import db
from .money import parse_amount

SCHEMA = """
CREATE TABLE IF NOT EXISTS banco_extractos (
    id INTEGER PRIMARY KEY, iban TEXT, archivo TEXT, formato TEXT, desde TEXT, hasta TEXT, saldo_inicial_cents INTEGER, saldo_final_cents INTEGER,
    movimientos INTEGER, nuevos INTEGER, cuadra INTEGER, avisos TEXT, usuario TEXT, importado_en TEXT);
CREATE TABLE IF NOT EXISTS banco_movimientos (
    id INTEGER PRIMARY KEY, extracto_id INTEGER, iban TEXT, fecha TEXT NOT NULL, fecha_valor TEXT, importe_cents INTEGER NOT NULL,
    concepto TEXT, referencia TEXT, documento TEXT, concepto_comun TEXT, huella TEXT UNIQUE NOT NULL,
    estado TEXT NOT NULL DEFAULT 'pendiente', tipo TEXT, destino TEXT, motivo TEXT, conciliado_por TEXT, conciliado_en TEXT);
CREATE INDEX IF NOT EXISTS ix_bm_estado ON banco_movimientos(estado, fecha);
"""
TIPOS = {"remesa": "Remesa SEPA", "remesa_item": "Pago de una remesa", "factura_proveedor": "Pago de factura de proveedor",
         "cobro_factura": "Cobro de factura emitida", "cobro_retencion": "Cobro de retención de garantía", "otro": "Otro (sin contrapartida)"}
DIAS_REMESA = (-5, 10)


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def _plano(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9 ]", " ", t)


_STOP = {"SL", "SLU", "SA", "S", "L", "U", "Y", "DE", "LA", "EL", "LOS", "LAS", "CB", "SC", "SOCIEDAD", "LIMITADA", "ANONIMA", "TRANSF",
         "TRANSFERENCIA", "RECIBO", "PAGO", "COBRO", "FAVOR", "ORD", "ORDENANTE", "BENEF", "FRA", "FACTURA", "SEPA"}


def _tokens(t: str) -> set[str]:
    return {w for w in _plano(t).split() if len(w) >= 3 and w not in _STOP}


def parecido_nombre(nombre: str, concepto: str) -> float:
    a, b = _tokens(nombre), _tokens(concepto)
    return len(a & b) / len(a) if a else 0.0


# ============================================================================ lectura de extractos
def _n43_fecha(s: str) -> str:
    return f"20{s[0:2]}-{s[2:4]}-{s[4:6]}"


def _n43_imp(s: str) -> int:
    return int(s)            # 14 dígitos con 2 decimales implícitos = céntimos


def leer_norma43(data: bytes) -> dict:
    txt = data.decode("latin-1")
    cuentas, avisos = [], []
    cur = None
    for n, raw in enumerate(txt.splitlines(), 1):
        ln = raw.rstrip("\r\n")
        if not ln.strip():
            continue
        ln = ln.ljust(80)
        tipo = ln[0:2]
        if tipo == "11":
            cur = {"iban_parcial": f"{ln[2:6]}{ln[6:10]}{ln[10:20]}", "entidad": ln[2:6], "oficina": ln[6:10], "cuenta": ln[10:20],
                   "desde": _n43_fecha(ln[20:26]), "hasta": _n43_fecha(ln[26:32]),
                   "saldo_inicial": _n43_imp(ln[33:47]) * (-1 if ln[32] == "1" else 1), "divisa": ln[47:50], "titular": ln[51:77].strip(),
                   "movs": [], "fin": None}
            cuentas.append(cur)
        elif tipo == "22" and cur is not None:
            signo = -1 if ln[27] == "1" else 1
            cur["movs"].append({"fecha": _n43_fecha(ln[10:16]), "fecha_valor": _n43_fecha(ln[16:22]), "concepto_comun": ln[22:24],
                                "importe_cents": signo * _n43_imp(ln[28:42]), "documento": ln[42:52].strip(),
                                "referencia": (ln[52:64].strip() + " " + ln[64:80].strip()).strip(), "concepto": ""})
        elif tipo == "23" and cur is not None and cur["movs"]:
            extra = (ln[4:42].strip() + " " + ln[42:80].strip()).strip()
            m = cur["movs"][-1]
            m["concepto"] = (m["concepto"] + " " + extra).strip()
        elif tipo == "33" and cur is not None:
            cur["fin"] = {"n_debe": int(ln[20:25]), "debe": _n43_imp(ln[25:39]), "n_haber": int(ln[39:44]), "haber": _n43_imp(ln[44:58]),
                          "saldo_final": _n43_imp(ln[59:73]) * (-1 if ln[58] == "1" else 1)}
        elif tipo in ("88", "24"):
            continue
        elif tipo not in ("11", "22", "23", "33"):
            avisos.append(f"Línea {n}: registro «{tipo}» desconocido")
    if not cuentas:
        raise ValueError("No es un fichero Norma 43 (no hay registro de cabecera 11).")
    for c in cuentas:
        debe = -sum(m["importe_cents"] for m in c["movs"] if m["importe_cents"] < 0)
        haber = sum(m["importe_cents"] for m in c["movs"] if m["importe_cents"] > 0)
        c["cuadra"] = True
        if c["fin"]:
            f = c["fin"]
            if f["debe"] != debe or f["haber"] != haber:
                c["cuadra"] = False
                avisos.append(f"Cuenta {c['cuenta']}: los totales del extracto (cargos {f['debe'] / 100:.2f}, abonos {f['haber'] / 100:.2f}) "
                              f"no coinciden con la suma de movimientos ({debe / 100:.2f}, {haber / 100:.2f}).")
            if c["saldo_inicial"] + haber - debe != f["saldo_final"]:
                c["cuadra"] = False
                avisos.append(f"Cuenta {c['cuenta']}: saldo inicial + abonos − cargos no da el saldo final.")
            c["saldo_final"] = f["saldo_final"]
        else:
            avisos.append(f"Cuenta {c['cuenta']}: falta el registro final (33); no se puede comprobar el saldo.")
            c["saldo_final"] = c["saldo_inicial"] + haber - debe
    return {"formato": "Norma 43", "cuentas": cuentas, "avisos": avisos}


def leer_tabla(data: bytes, nombre: str) -> dict:
    import pandas as pd
    if nombre.lower().endswith(".csv") or nombre.lower().endswith(".txt"):
        df = pd.read_csv(io.BytesIO(data), sep=None, engine="python", dtype=str, encoding="latin-1")
    else:
        crudo = pd.read_excel(io.BytesIO(data), dtype=str, header=None)
        fila_cab = 0
        for i in range(min(25, len(crudo))):                    # los bancos ponen varias filas de cabecera antes de la tabla
            vals = " ".join(_plano(str(v)) for v in crudo.iloc[i].tolist())
            if "FECHA" in vals and ("IMPORTE" in vals or "CARGO" in vals or "DEBE" in vals):
                fila_cab = i
                break
        df = pd.read_excel(io.BytesIO(data), dtype=str, header=fila_cab)
    cols = {c: _plano(str(c)).strip() for c in df.columns}

    def buscar(*claves, excluir=()):
        for c, n in cols.items():
            if any(k in n for k in claves) and not any(e in n for e in excluir):
                return c
        return None
    c_fecha = buscar("FECHA OPER", "F OPERACION", "FECHA CONT", "FECHA", excluir=("VALOR",))
    c_valor = buscar("VALOR")
    c_imp = buscar("IMPORTE", "CANTIDAD", excluir=("SALDO",))
    c_cargo = buscar("CARGO", "DEBE", "ADEUDO")
    c_abono = buscar("ABONO", "HABER", "INGRESO")
    c_conc = buscar("CONCEPTO", "DESCRIPCION", "DETALLE", "MOVIMIENTO")
    c_ref = buscar("REFERENCIA", "DOCUMENTO")
    if not c_fecha or not (c_imp or (c_cargo and c_abono)):
        raise ValueError("No encuentro las columnas de fecha e importe (o cargo y abono) en la hoja.")

    def fecha(v):
        v = str(v).strip()[:10]
        m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", v)
        if m:
            y = int(m.group(3))
            return date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1))).isoformat()
        return date.fromisoformat(v).isoformat()

    def imp(v):
        v = str(v or "").strip()
        if not v or v.lower() == "nan":
            return 0
        return int((parse_amount(v) * 100).to_integral_value())
    movs, avisos = [], []
    for i, r in enumerate(df.to_dict("records"), 1):
        if not str(r.get(c_fecha) or "").strip() or str(r.get(c_fecha)).lower() == "nan":
            continue
        try:
            f = fecha(r[c_fecha])
        except Exception:
            continue
        try:
            if c_imp:
                im = imp(r[c_imp])
            else:
                im = imp(r[c_abono]) - abs(imp(r[c_cargo]))
        except Exception:
            avisos.append(f"Fila {i}: importe no válido")
            continue
        if im == 0:
            continue
        movs.append({"fecha": f, "fecha_valor": fecha(r[c_valor]) if c_valor and str(r.get(c_valor) or "").strip() not in ("", "nan") else f,
                     "importe_cents": im, "concepto": str(r.get(c_conc) or "").strip() if c_conc else "", "referencia": str(r.get(c_ref) or "").strip()
                     if c_ref else "", "documento": "", "concepto_comun": ""})
    if not movs:
        raise ValueError("La hoja no tiene movimientos reconocibles.")
    cuenta = {"iban_parcial": "", "cuenta": "", "desde": min(m["fecha"] for m in movs), "hasta": max(m["fecha"] for m in movs),
              "saldo_inicial": None, "saldo_final": None, "movs": movs, "cuadra": None, "titular": ""}
    return {"formato": "Excel/CSV", "cuentas": [cuenta], "avisos": avisos}


def leer_extracto(data: bytes, nombre: str) -> dict:
    cab = data[:2]
    if cab == b"11" and not nombre.lower().endswith((".xlsx", ".xls", ".csv")):
        return leer_norma43(data)
    return leer_tabla(data, nombre)


def importar(con, data: bytes, nombre: str, usuario: str, iban: str = "") -> dict:
    ext = leer_extracto(data, nombre)
    nuevos = total = 0
    ids = []
    with db.tx(con):
        for c in ext["cuentas"]:
            cta = iban or c.get("iban_parcial") or ""
            cur = con.execute("""INSERT INTO banco_extractos (iban, archivo, formato, desde, hasta, saldo_inicial_cents, saldo_final_cents, movimientos,
                                 cuadra, avisos, usuario, importado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (cta, nombre, ext["formato"], c["desde"], c["hasta"], c.get("saldo_inicial"), c.get("saldo_final"), len(c["movs"]),
                               None if c.get("cuadra") is None else int(c["cuadra"]), json.dumps(ext["avisos"], ensure_ascii=False), usuario, db.now_iso()))
            eid = cur.lastrowid
            vistos = {}
            n_ext = 0
            for m in c["movs"]:
                base = f"{cta}|{m['fecha']}|{m['fecha_valor']}|{m['importe_cents']}|{_plano(m['concepto'])}|{m['referencia']}|{m.get('documento', '')}"
                vistos[base] = vistos.get(base, 0) + 1
                h = hashlib.sha256(f"{base}|{vistos[base]}".encode()).hexdigest()
                total += 1
                r = con.execute("""INSERT OR IGNORE INTO banco_movimientos (extracto_id, iban, fecha, fecha_valor, importe_cents, concepto, referencia,
                                   documento, concepto_comun, huella) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                (eid, cta, m["fecha"], m["fecha_valor"], m["importe_cents"], m["concepto"], m["referencia"], m.get("documento"),
                                 m.get("concepto_comun"), h))
                if r.rowcount:
                    nuevos += 1
                    n_ext += 1
                    ids.append(r.lastrowid)
            con.execute("UPDATE banco_extractos SET nuevos=? WHERE id=?", (n_ext, eid))
        db.audit(con, usuario, "extracto_bancario", "banco", None, {"archivo": nombre, "movimientos": total, "nuevos": nuevos})
    aplicados = 0
    if db.get_setting(con, "conc_auto", "1") == "1":
        aplicados = aplicar_automaticas(con, "conciliación automática")
    return {"formato": ext["formato"], "movimientos": total, "nuevos": nuevos, "repetidos": total - nuevos, "avisos": ext["avisos"],
            "cuadra": all(c.get("cuadra") is not False for c in ext["cuentas"]), "aplicados": aplicados}


# ============================================================================ propuestas
def _fecha(s: str) -> date:
    return date.fromisoformat(s[:10])


def candidatos(con, mov: dict) -> list[dict]:
    """Casaciones posibles de un movimiento, de mayor a menor confianza."""
    out = []
    imp = mov["importe_cents"]
    f = _fecha(mov["fecha"])
    texto = f"{mov.get('concepto') or ''} {mov.get('referencia') or ''} {mov.get('documento') or ''}"
    plano = _plano(texto)
    if imp < 0:
        a = -imp
        for r in db.rows(con, "SELECT * FROM remesas WHERE estado='generada' AND total_cents=?", (a,)):
            d = (f - _fecha(r["fecha_ejecucion"])).days
            if DIAS_REMESA[0] <= d <= DIAS_REMESA[1]:
                conf = 0.97 if r["referencia"] and r["referencia"] in plano.replace(" ", "") else 0.93
                out.append({"tipo": "remesa", "destino": {"remesa_id": r["id"]}, "confianza": conf,
                            "texto": f"Remesa {r['referencia']} ({r['num_pagos']} pagos, ejecución {r['fecha_ejecucion']})"})
        for r in db.rows(con, """SELECT ri.remesa_id, ri.documento_id, ri.importe_cents, r.referencia, r.fecha_ejecucion, d.numero,
                                        COALESCE(p.nombre, d.emisor_nombre) prov FROM remesa_items ri JOIN remesas r ON r.id=ri.remesa_id
                                 JOIN documentos d ON d.id=ri.documento_id LEFT JOIN proveedores p ON p.id=d.proveedor_id
                                 WHERE r.estado='generada' AND ri.importe_cents=?""", (a,)):
            d = (f - _fecha(r["fecha_ejecucion"])).days
            sim = parecido_nombre(r["prov"] or "", texto)
            if DIAS_REMESA[0] <= d <= DIAS_REMESA[1] and (sim >= 0.5 or (r["numero"] and _plano(r["numero"]).replace(" ", "") in plano.replace(" ", ""))):
                out.append({"tipo": "remesa_item", "destino": {"remesa_id": r["remesa_id"], "documento_id": r["documento_id"]},
                            "confianza": 0.85 + 0.1 * min(sim, 1), "texto": f"Pago a {r['prov']} (fra. {r['numero']}) de la remesa {r['referencia']}"})
        for r in db.rows(con, """SELECT d.id, d.numero, d.fecha, d.total_a_pagar_cents, COALESCE(p.nombre, d.emisor_nombre) prov FROM documentos d
                                 LEFT JOIN proveedores p ON p.id=d.proveedor_id WHERE d.estado='aprobada' AND COALESCE(d.pagada,0)=0
                                 AND d.total_a_pagar_cents=? AND d.id NOT IN (SELECT ri.documento_id FROM remesa_items ri JOIN remesas r ON r.id=ri.remesa_id
                                 WHERE r.estado='generada')""", (a,)):
            sim = parecido_nombre(r["prov"] or "", texto)
            cita = bool(r["numero"]) and _plano(r["numero"]).replace(" ", "") in plano.replace(" ", "")
            conf = 0.6 + 0.3 * min(sim, 1) + (0.08 if cita else 0)
            out.append({"tipo": "factura_proveedor", "destino": {"documento_id": r["id"]}, "confianza": round(min(conf, 0.98), 2),
                        "texto": f"Factura {r['numero']} de {r['prov']} ({r['fecha']})"})
    else:
        for fe in db.rows(con, "SELECT id FROM facturas_emitidas WHERE estado='emitida'"):
            from .tesoreria import estado_factura
            e = estado_factura(con, fe["id"])
            cita = bool(e["codigo"]) and _plano(e["codigo"]).replace(" ", "") in plano.replace(" ", "")
            sim = parecido_nombre(e["cliente"] or "", texto)
            for concepto, pend in (("factura", e["pendiente_c"]), ("retencion", e["pendiente_ret_c"])):
                if pend > 0 and pend == imp:
                    conf = 0.6 + (0.3 if cita else 0) + 0.1 * min(sim, 1)
                    out.append({"tipo": "cobro_factura" if concepto == "factura" else "cobro_retencion",
                                "destino": {"factura_id": e["id"], "concepto": concepto}, "confianza": round(min(conf, 0.99), 2),
                                "texto": f"{'Cobro' if concepto == 'factura' else 'Retención'} de la factura {e['codigo']} a {e['cliente']}"})
    return sorted(out, key=lambda x: -x["confianza"])


def pendientes(con, iban: str | None = None) -> list[dict]:
    q, p = "SELECT * FROM banco_movimientos WHERE estado='pendiente'", []
    if iban:
        q += " AND iban=?"; p.append(iban)
    out = []
    for m in db.rows(con, q + " ORDER BY fecha, id", p):
        m["candidatos"] = candidatos(con, m)
        out.append(m)
    return out


def conciliar(con, mov_id: int, cand: dict, usuario: str) -> str:
    m = db.one(con, "SELECT * FROM banco_movimientos WHERE id=?", (mov_id,))
    if not m or m["estado"] != "pendiente":
        raise ValueError("El movimiento ya no está pendiente.")
    t, d = cand["tipo"], cand["destino"]
    from . import pagos, tesoreria
    if t == "remesa":
        r = db.one(con, "SELECT * FROM remesas WHERE id=?", (d["remesa_id"],))
        if r["estado"] != "generada":
            raise ValueError("La remesa ya no está pendiente de confirmar.")
        n = pagos.confirmar(con, r["id"], m["fecha"], usuario)
        res = f"Remesa {r['referencia']} confirmada: {n} factura(s) pagadas el {m['fecha']}"
    elif t == "remesa_item":
        con.execute("UPDATE documentos SET pagada=1, fecha_pago=? WHERE id=?", (m["fecha"], d["documento_id"]))
        res = "Pago de la remesa identificado"
        faltan = db.one(con, """SELECT COUNT(*) n FROM remesa_items ri JOIN documentos x ON x.id=ri.documento_id
                                WHERE ri.remesa_id=? AND COALESCE(x.pagada,0)=0""", (d["remesa_id"],))["n"]
        if not faltan:
            pagos.confirmar(con, d["remesa_id"], m["fecha"], usuario)
            res += "; ya están todos sus pagos: remesa confirmada"
    elif t == "factura_proveedor":
        doc = db.one(con, "SELECT pagada FROM documentos WHERE id=?", (d["documento_id"],))
        if doc["pagada"]:
            raise ValueError("La factura ya consta como pagada.")
        con.execute("UPDATE documentos SET pagada=1, fecha_pago=? WHERE id=?", (m["fecha"], d["documento_id"]))
        db.audit(con, usuario, "pago_por_banco", "documento", d["documento_id"], {"movimiento": mov_id})
        res = "Factura marcada como pagada"
    elif t in ("cobro_factura", "cobro_retencion"):
        tesoreria.cobrar(con, d["factura_id"], m["fecha"], Decimal(m["importe_cents"]) / 100, d["concepto"], "transferencia", usuario,
                         notas=f"Conciliación bancaria, movimiento #{mov_id}")
        cob = db.one(con, "SELECT MAX(id) id FROM cobros WHERE factura_id=?", (d["factura_id"],))
        d = {**d, "cobro_id": cob["id"]}
        res = "Cobro registrado"
    else:
        raise ValueError("Tipo de conciliación desconocido.")
    with db.tx(con):
        con.execute("UPDATE banco_movimientos SET estado='conciliado', tipo=?, destino=?, conciliado_por=?, conciliado_en=? WHERE id=?",
                    (t, json.dumps(d), usuario, db.now_iso(), mov_id))
        db.audit(con, usuario, "conciliar", "banco_movimiento", mov_id, {"tipo": t, "destino": d})
    return res


def marcar_otro(con, mov_id: int, motivo: str, usuario: str) -> None:
    if not motivo.strip():
        raise ValueError("Indique el motivo (comisión, nómina, impuesto…).")
    with db.tx(con):
        con.execute("UPDATE banco_movimientos SET estado='otro', tipo='otro', motivo=?, conciliado_por=?, conciliado_en=? WHERE id=? AND estado='pendiente'",
                    (motivo.strip(), usuario, db.now_iso(), mov_id))
        db.audit(con, usuario, "movimiento_sin_contrapartida", "banco_movimiento", mov_id, {"motivo": motivo})


def deshacer(con, mov_id: int, usuario: str) -> None:
    m = db.one(con, "SELECT * FROM banco_movimientos WHERE id=?", (mov_id,))
    if m["estado"] == "pendiente":
        return
    d = json.loads(m["destino"] or "{}")
    with db.tx(con):
        if m["tipo"] == "remesa":
            con.execute("UPDATE remesas SET estado='generada', confirmado_por=NULL, confirmado_en=NULL WHERE id=?", (d["remesa_id"],))
            con.execute("UPDATE documentos SET pagada=0, fecha_pago=NULL WHERE id IN (SELECT documento_id FROM remesa_items WHERE remesa_id=?)",
                        (d["remesa_id"],))
        elif m["tipo"] in ("remesa_item", "factura_proveedor"):
            con.execute("UPDATE documentos SET pagada=0, fecha_pago=NULL WHERE id=?", (d["documento_id"],))
            if m["tipo"] == "remesa_item":
                con.execute("UPDATE remesas SET estado='generada', confirmado_por=NULL, confirmado_en=NULL WHERE id=? AND estado='pagada'", (d["remesa_id"],))
        elif m["tipo"] in ("cobro_factura", "cobro_retencion") and d.get("cobro_id"):
            con.execute("DELETE FROM cobros WHERE id=?", (d["cobro_id"],))
            if m["tipo"] == "cobro_retencion":
                con.execute("UPDATE facturas_emitidas SET retencion_estado='retenida' WHERE id=?", (d["factura_id"],))
        con.execute("UPDATE banco_movimientos SET estado='pendiente', tipo=NULL, destino=NULL, motivo=NULL, conciliado_por=NULL, conciliado_en=NULL WHERE id=?",
                    (mov_id,))
        db.audit(con, usuario, "deshacer_conciliacion", "banco_movimiento", mov_id, {"tipo": m["tipo"], "destino": d})


def aplicar_automaticas(con, usuario: str, umbral: float = 0.93) -> int:
    """Aplica solo las casaciones inequívocas: una candidata con confianza alta, o la mejor muy por delante de la segunda,
    y nunca la misma contrapartida para dos movimientos."""
    n = 0
    usados = set()
    for m in pendientes(con):
        c = m["candidatos"]
        if not c or c[0]["confianza"] < umbral:
            continue
        if len(c) > 1 and c[1]["confianza"] >= c[0]["confianza"] - 0.1:
            continue
        clave = json.dumps(c[0]["destino"], sort_keys=True)
        if clave in usados:
            continue
        try:
            conciliar(con, m["id"], c[0], usuario)
            usados.add(clave)
            n += 1
        except ValueError:
            continue
    return n


def resumen(con) -> dict:
    r = db.one(con, """SELECT COUNT(*) n, SUM(estado='pendiente') pend, SUM(estado='conciliado') conc, SUM(estado='otro') otros,
                       MIN(CASE WHEN estado='pendiente' THEN fecha END) mas_antiguo FROM banco_movimientos""") or {}
    return {"movimientos": r.get("n") or 0, "pendientes": r.get("pend") or 0, "conciliados": r.get("conc") or 0, "otros": r.get("otros") or 0,
            "mas_antiguo": r.get("mas_antiguo")}


def generar_norma43_prueba(movs: list[tuple[str, int, str]], saldo_inicial: int = 0, cuenta: str = "0123456789") -> bytes:
    """Norma 43 sintético (para pruebas y demostraciones). movs = [(AAAA-MM-DD, céntimos con signo, concepto)]."""
    def f(d):
        return d[2:4] + d[5:7] + d[8:10]
    lineas = []
    desde, hasta = min(m[0] for m in movs), max(m[0] for m in movs)
    lineas.append(f"11{'2100'}{'0001'}{cuenta:>10}{f(desde)}{f(hasta)}{'1' if saldo_inicial < 0 else '2'}{abs(saldo_inicial):014d}978{'3'}{'LLORCA GROUP':<26}".ljust(80))
    debe = haber = nd = nh = 0
    for d, imp, conc in movs:
        clave = "1" if imp < 0 else "2"
        if imp < 0:
            debe += -imp; nd += 1
        else:
            haber += imp; nh += 1
        lineas.append(f"22{'    '}{'0001'}{f(d)}{f(d)}{'99'}{'000'}{clave}{abs(imp):014d}{'0000000000'}{'':<12}{'':<16}".ljust(80))
        lineas.append(f"2301{conc[:38]:<38}{conc[38:76]:<38}".ljust(80))
    final = saldo_inicial + haber - debe
    lineas.append(f"33{'2100'}{'0001'}{cuenta:>10}{nd:05d}{debe:014d}{nh:05d}{haber:014d}{'1' if final < 0 else '2'}{abs(final):014d}978".ljust(80))
    lineas.append(f"88{'9' * 18}{len(lineas) - 0:06d}".ljust(80))
    return ("\r\n".join(lineas) + "\r\n").encode("latin-1")
