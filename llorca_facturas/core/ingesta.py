"""
Ingesta: alta de PDFs, aplicación de la extracción IA y guardado de revisiones.
Principio: la IA propone, la persona valida. Todo cambio queda auditado.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import db, maestros
from .fiscal import normalize_nif, normalize_iban
from .money import D, to_cents, ZERO
from .pdf_utils import store_pdf, read_text, has_text_layer, sha256
from .validation import normalizar_numero_factura, validar_y_guardar


def _huella_texto(texto: str) -> str | None:
    """Huella del contenido: el mismo documento reenviado o reexportado (bytes distintos, mismo texto) se reconoce."""
    import hashlib
    import re as _re
    t = _re.sub(r"\s+", "", (texto or "").lower())
    return hashlib.sha256(t.encode()).hexdigest() if len(t) > 200 else None


MAX_MB = 60


def comprobar_archivo(filename: str, data: bytes) -> str | None:
    """Errores típicos de subida: archivo vacío, no es PDF, protegido con contraseña, dañado o desmesurado."""
    if not data or len(data) < 200:
        return "archivo vacío o incompleto"
    if len(data) > MAX_MB * 1024 * 1024:
        return f"supera {MAX_MB} MB"
    if data[:5] != b"%PDF-":
        return "no es un PDF válido (¿extensión cambiada?)"
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        if len(pdf) == 0:
            return "PDF sin páginas"
        if len(pdf) > 300:
            return f"PDF de {len(pdf)} páginas: parece un documento agrupado, sepárelo"
    except Exception as e:  # noqa: BLE001
        txt = str(e).lower()
        return "PDF protegido con contraseña" if "password" in txt else "PDF dañado o ilegible"
    return None


CUENTAS_PGC = {
    "600": "600 · Compras de mercaderías", "601": "601 · Compras de materias primas y materiales",
    "602": "602 · Compras de otros aprovisionamientos", "607": "607 · Trabajos realizados por otras empresas (subcontratas)",
    "621": "621 · Arrendamientos y cánones (alquileres, grúas)", "622": "622 · Reparaciones y conservación",
    "623": "623 · Servicios de profesionales independientes", "624": "624 · Transportes", "625": "625 · Primas de seguros",
    "628": "628 · Suministros", "629": "629 · Otros servicios",
}


def proponer_cuenta(con, doc_id: int) -> tuple[str, str]:
    """Cuenta contable propuesta: la habitual del proveedor (aprendida de lo aprobado) o por reglas del contenido."""
    d = db.one(con, "SELECT proveedor_id, irpf_cents, inversion_sujeto_pasivo FROM documentos WHERE id=?", (doc_id,))
    if d and d["proveedor_id"]:
        r = db.one(con, """SELECT cuenta_contable, COUNT(*) n FROM documentos WHERE proveedor_id=? AND id<>? AND estado='aprobada'
                           AND cuenta_contable IS NOT NULL GROUP BY cuenta_contable ORDER BY n DESC LIMIT 1""", (d["proveedor_id"], doc_id))
        if r:
            return r["cuenta_contable"], "habitual del proveedor"
    txt = " ".join((r["descripcion"] or "").lower() for r in db.rows(con, "SELECT descripcion FROM lineas WHERE documento_id=?", (doc_id,)))
    if d and d["irpf_cents"]:
        return "623", "lleva retención de IRPF (profesional)"
    if re.search(r"alquiler|grua|gr[uú]a|andamio|plataforma elevadora|contenedor", txt):
        return "621", "alquileres y medios"
    if re.search(r"\bporte|transporte", txt) and not re.search(r"mortero|saco|ladrillo|material", txt):
        return "624", "transporte"
    if d and d["inversion_sujeto_pasivo"] or re.search(r"mano de obra|\bhoras?\b|trabajos|certificaci[oó]n|montaje|instalaci[oó]n", txt):
        return "607", "ejecución de obra por subcontrata"
    if re.search(r"electricidad|agua|gas|telefon", txt):
        return "628", "suministros"
    return "601", "compra de materiales"


def _cols_extra(con):
    for col in ("texto_hash TEXT", "obra_lote INTEGER", "obra_forzada INTEGER DEFAULT 0", "estado_previo TEXT",
                "cuenta_contable TEXT", "conformado_por TEXT", "conformado_en TEXT", "idioma TEXT", "moneda TEXT",
                "centro_coste TEXT"):
        try:
            con.execute(f"ALTER TABLE documentos ADD COLUMN {col}")
        except Exception:
            pass


def registrar_pdf(con, filename: str, data: bytes, usuario: str, obra_id: int | None = None,
                  forzar_obra: bool = False, lote: str | None = None) -> tuple[int, bool]:
    """Alta del PDF. Devuelve (doc_id, es_nuevo). Un PDF idéntico (mismo SHA-256) nunca entra dos veces; uno con el
    mismo contenido y distinto archivo se registra como «duplicado» y no se lee ni computa."""
    _cols_extra(con)
    h = sha256(data)
    ex = db.one(con, "SELECT id FROM documentos WHERE file_hash=?", (h,))
    if ex:
        return ex["id"], False
    h, path = store_pdf(data)
    texto, paginas = read_text(data)
    th = _huella_texto(texto)
    gemelo = db.one(con, "SELECT id, filename FROM documentos WHERE texto_hash=? AND texto_hash IS NOT NULL AND estado<>'eliminado'",
                    (th,)) if th else None
    with db.tx(con):
        cur = con.execute(
            "INSERT INTO documentos (file_hash, filename, file_path, paginas, tiene_texto, texto, estado) VALUES (?,?,?,?,?,?,?)",
            (h, filename, str(path), paginas, int(has_text_layer(texto)), texto, "duplicado" if gemelo else "sin_procesar"))
        doc_id = cur.lastrowid
        try:
            con.execute("ALTER TABLE documentos ADD COLUMN lote_subida TEXT")
        except Exception:
            pass
        con.execute("UPDATE documentos SET texto_hash=?, notas=?, obra_lote=?, obra_forzada=?, lote_subida=? WHERE id=?",
                    (th, f"Mismo contenido que el documento #{gemelo['id']} ({gemelo['filename']})" if gemelo else "",
                     obra_id, int(bool(forzar_obra and obra_id)), lote, doc_id))
        try:
            from . import indice
            indice.indexar(con, doc_id, indice.paginas_pdf(data))
        except Exception:
            pass
        db.audit(con, usuario, "alta_pdf", "documento", doc_id, {"filename": filename, "sha256": h,
                                                                 "duplicado_de": gemelo and gemelo["id"]})
    return doc_id, True


def _cents_or_none(v):
    if v is None or v == "":
        return None
    return to_cents(D(v))


def _norm_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def aplicar_extraccion(con, doc_id: int, res: dict, usuario: str, obra_defecto: int | None = None) -> None:
    from .cierres import comprobar_documento
    comprobar_documento(con, doc_id)
    from . import historial as _h
    _h.init(con)
    _h.guardar_version(con, doc_id, usuario, "antes de leer")
    con.commit()
    d = res["data"]
    aviso_ia = d.pop("_aviso_ia", None)
    if aviso_ia:
        res["modelo"] = (res.get("modelo") or "") + " (IA local no disponible: solo reglas)"
    if res.get("metodo_texto") == "ocr" and res.get("texto"):
        # el texto OCR se guarda: permite buscar y anclar importes también en escaneados
        con.execute("UPDATE documentos SET texto=?, tiene_texto=1 WHERE id=? AND tiene_texto=0", (res["texto"], doc_id))
        try:
            from . import indice
            indice.indexar(con, doc_id, res["texto"].split("\f"))
        except Exception:
            pass
    emisor, receptor, obra = d.get("emisor") or {}, d.get("receptor") or {}, d.get("obra") or {}
    fecha = _norm_date(d.get("fecha"))

    prov_id = maestros.upsert_proveedor(con, emisor.get("nif"), emisor.get("nombre"))
    obra_txt = " ".join(x for x in [obra.get("texto_literal"), obra.get("codigo"), d.get("concepto_general")] if x)
    obra_id, obra_conf, _motivo = maestros.identificar_obra(con, obra_txt)
    if obra_id and obra_conf < 0.99:
        doc_txt0 = db.one(con, "SELECT texto FROM documentos WHERE id=?", (doc_id,))["texto"] or ""
        oid2, oc2, _ = maestros.identificar_obra(con, doc_txt0[:20000])
        if oid2 == obra_id and oc2 >= 0.99:
            obra_conf = 0.95
    if not obra_id and obra.get("codigo"):
        r = db.one(con, "SELECT id FROM obras WHERE codigo=?", (str(obra["codigo"]).strip(),))
        if r:
            obra_id, obra_conf = r["id"], 0.75
    if not obra_id:
        # Último recurso: buscar en el texto completo del PDF
        doc_txt = db.one(con, "SELECT texto FROM documentos WHERE id=?", (doc_id,))["texto"] or ""
        oid, oc, _ = maestros.identificar_obra(con, doc_txt[:20000])
        if oid:
            obra_id, obra_conf = oid, (0.9 if oc >= 0.99 else min(oc, 0.7))
    _cols_extra(con)
    meta = db.one(con, "SELECT obra_lote, obra_forzada, texto FROM documentos WHERE id=?", (doc_id,)) or {}
    lote = meta.get("obra_lote") or obra_defecto
    obra_detectada = obra_id
    if meta.get("obra_forzada") and lote:
        obra_id, obra_conf = lote, 1.0           # el usuario forzó la obra al subir
    elif not obra_id and lote:
        obra_id, obra_conf = lote, 0.8           # no se detecta en el documento: la del lote
    # ¿menciona varias obras?
    obras_mencionadas = []
    for o_ in maestros.listar_obras(con, solo_activas=False):
        if re.search(rf"(?<![\d.,/]){re.escape(o_['codigo'])}(?![\d.,/])", meta.get("texto") or ""):
            obras_mencionadas.append(o_["codigo"])

    ret = d.get("retencion_garantia") or {}
    irpf = d.get("irpf") or {}
    iban = normalize_iban(emisor.get("iban"))
    total_a_pagar = d.get("total_a_pagar")
    if total_a_pagar in (None, "") and d.get("total_factura") not in (None, ""):
        total_a_pagar = d.get("total_factura")

    ya_dup = (db.one(con, "SELECT estado FROM documentos WHERE id=?", (doc_id,)) or {}).get("estado") == "duplicado"
    with db.tx(con):
        con.execute("""
            UPDATE documentos SET
              estado=CASE WHEN estado='duplicado' THEN 'duplicado' ELSE 'pendiente_revision' END, tipo_documento=?, proveedor_id=?, obra_id=?, obra_confianza=?,
              emisor_nombre=?, emisor_nif=?, receptor_nombre=?, receptor_nif=?,
              numero=?, numero_normalizado=?, fecha=?, fecha_vencimiento=?, periodo=?,
              referencia_obra_texto=?, pedido_contrato=?, presupuesto_ref=?, albaranes=?, factura_rectificada=?,
              concepto_general=?, importe_bruto_cents=?, base_imponible_cents=?,
              total_iva_cents=?, total_recargo_cents=?, total_factura_cents=?,
              irpf_pct=?, irpf_cents=?, ret_garantia_pct=?, ret_garantia_base_cents=?, ret_garantia_cents=?,
              total_a_pagar_cents=?, inversion_sujeto_pasivo=?, exencion_motivo=?, forma_pago=?, iban=?,
              confianza=?, campos_dudosos=?, observaciones_ia=?, extraccion_json=?, modelo=?,
              tokens_entrada=?, tokens_salida=?, extraido_en=?
            WHERE id=?""", (
            d.get("tipo_documento") or "factura", prov_id, obra_id, obra_conf,
            emisor.get("nombre"), normalize_nif(emisor.get("nif")) or None,
            receptor.get("nombre"), normalize_nif(receptor.get("nif")) or None,
            d.get("numero"), normalizar_numero_factura(d.get("numero")), fecha, _norm_date(d.get("fecha_vencimiento")),
            d.get("periodo"), obra.get("texto_literal"), d.get("pedido_contrato"), d.get("presupuesto_referencia"),
            json.dumps(d.get("albaranes") or [], ensure_ascii=False), d.get("factura_rectificada"),
            d.get("concepto_general"), _cents_or_none(d.get("importe_bruto")), _cents_or_none(d.get("base_imponible")),
            sum((to_cents(D(t.get("cuota"))) for t in d.get("impuestos") or []), 0),
            sum((to_cents(D(t.get("recargo_cuota"))) for t in d.get("impuestos") or []), 0),
            _cents_or_none(d.get("total_factura")),
            irpf.get("pct"), _cents_or_none(irpf.get("importe")) if irpf.get("importe") else None,
            ret.get("pct"), _cents_or_none(ret.get("base")),
            abs(_cents_or_none(ret.get("importe")) or 0) or None,
            _cents_or_none(total_a_pagar), int(bool(d.get("inversion_sujeto_pasivo"))), d.get("exencion_motivo"),
            d.get("forma_pago"), iban or None,
            d.get("confianza"), json.dumps(d.get("campos_dudosos") or [], ensure_ascii=False), d.get("observaciones"),
            json.dumps(d, ensure_ascii=False, default=str), res.get("modelo"), res.get("tokens_entrada"), res.get("tokens_salida"),
            db.now_iso(), doc_id))
        # IRPF: se guarda en positivo (es una detracción)
        con.execute("UPDATE documentos SET irpf_cents=ABS(irpf_cents) WHERE id=? AND irpf_cents IS NOT NULL", (doc_id,))

        con.execute("DELETE FROM impuestos WHERE documento_id=?", (doc_id,))
        for t in d.get("impuestos") or []:
            con.execute("INSERT INTO impuestos (documento_id, tipo_pct, base_cents, cuota_cents, recargo_pct, recargo_cents) "
                        "VALUES (?,?,?,?,?,?)",
                        (doc_id, str(D(t.get("tipo_pct") or 0)), to_cents(D(t.get("base"))), to_cents(D(t.get("cuota"))),
                         t.get("recargo_pct"), to_cents(D(t.get("recargo_cuota")))))

        con.execute("DELETE FROM lineas WHERE documento_id=?", (doc_id,))
        partidas = maestros.partidas_de_obra(con, obra_id) if obra_id else []
        for i, l in enumerate(d.get("lineas") or [], start=1):
            pid, origen, pconf = None, None, None
            if partidas:
                pid_ia = maestros.partida_por_codigo(partidas, l.get("partida_codigo"))
                conf_ia = float(l.get("partida_confianza") or 0)
                pid_kw, conf_kw = maestros.clasificar_linea(l.get("descripcion") or "", partidas)
                es_99 = str(l.get("partida_codigo") or "") == "99"
                etiqueta = "reglas" if str(res.get("modelo") or "").startswith(("local", "alta manual")) else "ia"
                if pid_ia and not es_99 and conf_ia >= 0.5:
                    pid, origen, pconf = pid_ia, etiqueta, conf_ia
                elif pid_kw:
                    pid, origen, pconf = pid_kw, "reglas", conf_kw
                elif pid_ia:
                    pid, origen, pconf = pid_ia, "ia", conf_ia
            con.execute(
                "INSERT INTO lineas (documento_id, orden, codigo, descripcion, cantidad, unidad, precio_unitario, "
                "descuento_pct, importe_cents, tipo_linea, es_extra, albaran, partida_id, partida_origen, partida_confianza) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, i, l.get("codigo"), l.get("descripcion"), l.get("cantidad"), l.get("unidad"),
                 l.get("precio_unitario"), l.get("descuento_pct"), to_cents(D(l.get("importe"))),
                 l.get("tipo_linea") or "normal", int(bool(l.get("es_extra")) or l.get("tipo_linea") == "extra"),
                 l.get("albaran"), pid, origen, pconf))
        if partidas:
            _completar_partidas(con, doc_id, prov_id, partidas)

        if prov_id and iban:
            maestros.registrar_iban(con, prov_id, iban, fecha or "")
        # duplicado real: mismo emisor, mismo nº de factura y mismo importe que otro documento vivo -> no computa
        dn = normalizar_numero_factura(d.get("numero"))
        nif_e = normalize_nif(emisor.get("nif"))
        if dn and nif_e:
            otro = db.one(con, """SELECT id, filename FROM documentos WHERE id<>? AND emisor_nif=? AND numero_normalizado=?
                                  AND estado NOT IN ('rechazada','duplicado','sin_procesar')
                                  AND COALESCE(total_a_pagar_cents,0)=COALESCE(?,0)""",
                          (doc_id, nif_e, dn, _cents_or_none(total_a_pagar)))
            if otro:
                con.execute("UPDATE documentos SET estado='duplicado', notas=? WHERE id=?",
                            (f"Duplicado de #{otro['id']} ({otro['filename']}): mismo emisor, número e importe", doc_id))
        db.audit(con, usuario, "extraccion_ia", "documento", doc_id,
                 {"modelo": res.get("modelo"), "tokens": [res.get("tokens_entrada"), res.get("tokens_salida")],
                  "segundos": res.get("segundos")})
    try:
        con.execute("UPDATE documentos SET idioma=?, moneda=? WHERE id=?", (d.get("idioma"), d.get("moneda") or "EUR", doc_id))
        cta, _mot = proponer_cuenta(con, doc_id)
        con.execute("UPDATE documentos SET cuenta_contable=COALESCE(cuenta_contable, ?) WHERE id=?", (cta, doc_id))
        cc = db.one(con, """SELECT o.codigo, (SELECT p.codigo FROM lineas l JOIN partidas p ON p.id=l.partida_id WHERE l.documento_id=d.id
                                              GROUP BY p.codigo ORDER BY SUM(ABS(l.importe_cents)) DESC LIMIT 1) AS cap
                            FROM documentos d LEFT JOIN obras o ON o.id=d.obra_id WHERE d.id=?""", (doc_id,))
        if cc and cc["codigo"]:
            con.execute("UPDATE documentos SET centro_coste=? WHERE id=? AND centro_coste IS NULL",
                        (cc["codigo"] + (f".{cc['cap']}" if cc["cap"] and cc["cap"] != "99" else ""), doc_id))
        con.commit()
    except Exception:
        pass
    tl_ = (meta.get("texto") or "")
    if re.search(r"%OR\b", tl_) and re.search(r"\bAN\b", tl_) and re.search(r"\bAC\b", tl_):
        con.execute("UPDATE documentos SET tipo_documento='certificacion' WHERE id=?", (doc_id,))
        con.commit()
    validar_y_guardar(con, doc_id)


def _completar_partidas(con, doc_id: int, prov_id: int | None, partidas: list[dict]) -> None:
    """Líneas sin partida: 1) la partida habitual de ese proveedor (aprendida de facturas anteriores revisadas),
    2) la partida dominante (por importe) de la propia factura. Siempre con confianza baja para que se revise."""
    sin = db.rows(con, "SELECT id FROM lineas WHERE documento_id=? AND partida_id IS NULL", (doc_id,))
    if not sin:
        return
    ids_obra = {p["id"] for p in partidas}
    habitual = None
    if prov_id:
        r = db.one(con, """SELECT l.partida_id, SUM(ABS(l.importe_cents)) s FROM lineas l JOIN documentos d ON d.id=l.documento_id
                           WHERE d.proveedor_id=? AND d.id<>? AND l.partida_id IS NOT NULL AND l.partida_origen IN ('manual','ia','reglas')
                           GROUP BY l.partida_id ORDER BY s DESC LIMIT 1""", (prov_id, doc_id))
        if r and r["partida_id"] in ids_obra:
            habitual = r["partida_id"]
    dom = db.one(con, """SELECT partida_id, SUM(ABS(importe_cents)) s FROM lineas WHERE documento_id=? AND partida_id IS NOT NULL
                         GROUP BY partida_id ORDER BY s DESC LIMIT 1""", (doc_id,))
    pid, origen = (habitual, "proveedor") if habitual else ((dom["partida_id"], "contexto") if dom else (None, None))
    if pid:
        con.execute("UPDATE lineas SET partida_id=?, partida_origen=?, partida_confianza=0.3 WHERE documento_id=? AND partida_id IS NULL",
                    (pid, origen, doc_id))


def a_papelera(con, doc_id: int, usuario: str) -> None:
    from . import historial as _h
    _h.init(con)
    _h.guardar_version(con, doc_id, usuario, "antes de enviar a la papelera")
    con.commit()
    with db.tx(con):
        con.execute("UPDATE documentos SET estado_previo=estado, estado='eliminado' WHERE id=? AND estado<>'eliminado'", (doc_id,))
        db.audit(con, usuario, "papelera", "documento", doc_id, None)


def restaurar(con, doc_id: int, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE documentos SET estado=COALESCE(estado_previo,'pendiente_revision'), estado_previo=NULL WHERE id=? "
                    "AND estado='eliminado'", (doc_id,))
        db.audit(con, usuario, "restaurar", "documento", doc_id, None)


def borrar_definitivo(con, doc_id: int, usuario: str) -> None:
    d = db.one(con, "SELECT filename, file_hash FROM documentos WHERE id=? AND estado='eliminado'", (doc_id,))
    if not d:
        raise ValueError("Solo se pueden borrar definitivamente documentos que estén en la papelera.")
    with db.tx(con):
        con.execute("DELETE FROM documentos WHERE id=?", (doc_id,))
        db.audit(con, usuario, "borrado_definitivo", "documento", doc_id, dict(d))


def deshacer_cabecera(con, doc_id: int, usuario: str) -> dict | None:
    """Revierte el último cambio de cabecera hecho a mano (los valores anteriores están en la auditoría)."""
    r = db.one(con, "SELECT id, detalle FROM auditoria WHERE entidad='documento' AND entidad_id=? AND accion='editar_cabecera' "
                    "ORDER BY id DESC LIMIT 1", (doc_id,))
    if not r:
        return None
    diff = json.loads(r["detalle"] or "{}")
    with db.tx(con):
        for k, v in diff.items():
            if re.fullmatch(r"[a-z_]+", k):
                con.execute(f"UPDATE documentos SET {k}=? WHERE id=?", (v["antes"], doc_id))
        con.execute("UPDATE auditoria SET accion='editar_cabecera_deshecho' WHERE id=?", (r["id"],))
        db.audit(con, usuario, "deshacer_cabecera", "documento", doc_id, diff)
    validar_y_guardar(con, doc_id)
    return diff


CAMPOS_EDITABLES_CENTS = {"importe_bruto_cents", "base_imponible_cents", "total_factura_cents", "irpf_cents",
                          "ret_garantia_base_cents", "ret_garantia_cents", "total_a_pagar_cents"}


def guardar_cabecera(con, doc_id: int, cambios: dict, usuario: str) -> dict:
    """Guarda cambios de cabecera con auditoría campo a campo (valor anterior → nuevo)."""
    from .cierres import comprobar_documento
    comprobar_documento(con, doc_id)
    from . import historial as _h
    _h.init(con)
    _h.guardar_version(con, doc_id, usuario, "antes de editar la cabecera")
    con.commit()
    antes = db.one(con, "SELECT * FROM documentos WHERE id=?", (doc_id,))
    diff = {}
    for k, v in cambios.items():
        if k in CAMPOS_EDITABLES_CENTS and v is not None and v != "":
            v = to_cents(D(v))
        if k in ("emisor_nif", "receptor_nif"):
            v = normalize_nif(v) or None
        if k == "iban":
            v = normalize_iban(v) or None
        if k == "numero":
            con.execute("UPDATE documentos SET numero_normalizado=? WHERE id=?", (normalizar_numero_factura(v), doc_id))
        if antes.get(k) != v:
            diff[k] = {"antes": antes.get(k), "despues": v}
    if not diff:
        return {}
    with db.tx(con):
        sets = ", ".join(f"{k}=?" for k in diff)
        con.execute(f"UPDATE documentos SET {sets} WHERE id=?", [diff[k]["despues"] for k in diff] + [doc_id])
        if "emisor_nif" in diff or "emisor_nombre" in diff:
            doc = db.one(con, "SELECT emisor_nif, emisor_nombre, iban, fecha FROM documentos WHERE id=?", (doc_id,))
            pid = maestros.upsert_proveedor(con, doc["emisor_nif"], doc["emisor_nombre"])
            con.execute("UPDATE documentos SET proveedor_id=? WHERE id=?", (pid, doc_id))
        if "iban" in diff:
            doc = db.one(con, "SELECT proveedor_id, iban, fecha FROM documentos WHERE id=?", (doc_id,))
            if doc["iban"] and doc["proveedor_id"]:
                maestros.registrar_iban(con, doc["proveedor_id"], doc["iban"], doc["fecha"] or "")
        if "obra_id" in diff:
            con.execute("UPDATE documentos SET obra_confianza=1.0 WHERE id=?", (doc_id,))
        db.audit(con, usuario, "editar_cabecera", "documento", doc_id, diff)
        if set(diff) - {"notas", "estado"}:
            _anular_aprobacion_si_procede(con, doc_id, usuario)
    validar_y_guardar(con, doc_id)
    return diff


def guardar_lineas(con, doc_id: int, lineas: list[dict], usuario: str) -> None:
    from .cierres import comprobar_documento
    comprobar_documento(con, doc_id)
    from . import historial as _h
    _h.init(con)
    _h.guardar_version(con, doc_id, usuario, "antes de editar las líneas")
    con.commit()
    antes = db.rows(con, "SELECT orden, descripcion, importe_cents, partida_id FROM lineas WHERE documento_id=?", (doc_id,))
    with db.tx(con):
        con.execute("DELETE FROM lineas WHERE documento_id=?", (doc_id,))
        for i, l in enumerate(lineas, start=1):
            if not (l.get("descripcion") or l.get("importe") not in (None, "")):
                continue
            con.execute(
                "INSERT INTO lineas (documento_id, orden, codigo, descripcion, cantidad, unidad, precio_unitario, descuento_pct, "
                "importe_cents, tipo_linea, es_extra, albaran, partida_id, partida_origen, partida_confianza) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, i, l.get("codigo"), l.get("descripcion"),
                 _dec_str(l.get("cantidad")), l.get("unidad"), _dec_str(l.get("precio_unitario")), _dec_str(l.get("descuento_pct")),
                 to_cents(D(l.get("importe") or 0)), l.get("tipo_linea") or "normal", int(bool(l.get("es_extra"))),
                 l.get("albaran") or None, l.get("partida_id"),
                 {"IA": "ia", "Reglas": "reglas", "Habitual proveedor": "proveedor", "Contexto factura": "contexto",
                  "Certificación": "cert"}.get(l.get("partida_origen"), l.get("partida_origen") or "manual"),
                 l.get("partida_confianza")))
        db.audit(con, usuario, "editar_lineas", "documento", doc_id, {"antes": antes, "n_despues": len(lineas)})
        _anular_aprobacion_si_procede(con, doc_id, usuario)
    validar_y_guardar(con, doc_id)


def guardar_impuestos(con, doc_id: int, impuestos: list[dict], usuario: str) -> None:
    from .cierres import comprobar_documento
    comprobar_documento(con, doc_id)
    from . import historial as _h
    _h.init(con)
    _h.guardar_version(con, doc_id, usuario, "antes de editar el IVA")
    con.commit()
    with db.tx(con):
        con.execute("DELETE FROM impuestos WHERE documento_id=?", (doc_id,))
        tot_iva = tot_rec = 0
        for t in impuestos:
            if t.get("base") in (None, ""):
                continue
            c = to_cents(D(t.get("cuota") or 0))
            r = to_cents(D(t.get("recargo") or 0))
            tot_iva += c
            tot_rec += r
            con.execute("INSERT INTO impuestos (documento_id, tipo_pct, base_cents, cuota_cents, recargo_pct, recargo_cents) "
                        "VALUES (?,?,?,?,?,?)", (doc_id, _dec_str(t.get("tipo_pct") or 0), to_cents(D(t.get("base"))), c,
                                                 _dec_str(t.get("recargo_pct")), r))
        con.execute("UPDATE documentos SET total_iva_cents=?, total_recargo_cents=? WHERE id=?", (tot_iva, tot_rec, doc_id))
        db.audit(con, usuario, "editar_impuestos", "documento", doc_id, impuestos)
        _anular_aprobacion_si_procede(con, doc_id, usuario)
    validar_y_guardar(con, doc_id)


def _anular_aprobacion_si_procede(con, doc_id: int, usuario: str) -> None:
    """Si se modifica un documento revisado, conformado o aprobado, esas firmas dejan de valer: vuelve a pendiente."""
    d = db.one(con, "SELECT estado, conformado_por FROM documentos WHERE id=?", (doc_id,))
    if d and (d["estado"] in ("revisada", "aprobada") or d["conformado_por"]):
        con.execute("UPDATE documentos SET estado=CASE WHEN estado IN ('revisada','aprobada') THEN 'pendiente_revision' ELSE estado END, "
                    "aprobado_por=NULL, aprobado_en=NULL, conformado_por=NULL, conformado_en=NULL WHERE id=?", (doc_id,))
        db.audit(con, usuario, "aprobacion_anulada_por_modificacion", "documento", doc_id,
                 {"estado_anterior": d["estado"], "conformidad_anterior": d["conformado_por"]})


def reglas_aprobacion(con) -> dict:
    return {"conformidad": db.get_setting(con, "exigir_conformidad", "1") == "1",
            "umbral_cents": int(db.get_setting(con, "umbral_direccion_cents", "1000000") or 1000000),
            "dias_aviso": int(db.get_setting(con, "dias_aviso_aprobacion", "15") or 15)}


def requisitos_aprobacion(con, doc_id: int, rol: str | None) -> list[str]:
    """Qué falta para poder aprobar este documento con este rol (lista vacía = se puede)."""
    d = db.one(con, "SELECT obra_id, base_imponible_cents, conformado_por FROM documentos WHERE id=?", (doc_id,))
    reglas = reglas_aprobacion(con)
    faltan = []
    if rol is not None and rol not in ("admin", "direccion", "gestor"):
        faltan.append("Su rol no puede aprobar facturas (solo dar la conformidad).")
    try:
        from .usuarios import jefes_de_obra
        jefes = jefes_de_obra(con, d["obra_id"]) if d and d["obra_id"] else []
    except Exception:
        jefes = []
    if reglas["conformidad"] and jefes and not d["conformado_por"]:
        faltan.append("Falta la conformidad del jefe de obra (" + ", ".join(j["nombre"] or j["usuario"] for j in jefes) + ").")
    if rol is not None and d and abs(d["base_imponible_cents"] or 0) >= reglas["umbral_cents"] and rol not in ("admin", "direccion"):
        faltan.append(f"Importe igual o superior a {reglas['umbral_cents'] / 100:,.0f} €: debe aprobarla Dirección.".replace(",", "."))
    return faltan


def conformar(con, doc_id: int, usuario: str, rol: str, obras_usuario: set | None = None) -> tuple[bool, str]:
    """Conformidad del jefe de obra: confirma que el trabajo o material se ha recibido y la imputación es correcta."""
    d = db.one(con, "SELECT obra_id, estado FROM documentos WHERE id=?", (doc_id,))
    if rol not in ("jefe_obra", "admin", "direccion"):
        return False, "Solo el jefe de obra (o Dirección) puede dar la conformidad."
    if rol == "jefe_obra" and (obras_usuario is None or d["obra_id"] not in obras_usuario):
        return False, "Esta factura no es de una de sus obras."
    from . import historial as _h
    _h.init(con)
    _h.guardar_version(con, doc_id, usuario, "antes de dar la conformidad")
    with db.tx(con):
        con.execute("UPDATE documentos SET conformado_por=?, conformado_en=? WHERE id=?", (usuario, db.now_iso(), doc_id))
        db.audit(con, usuario, "conformidad_obra", "documento", doc_id, None)
    return True, "Conformidad registrada"


def cambiar_estado(con, doc_id: int, estado: str, usuario: str, comentario: str = "", rol: str | None = None) -> tuple[bool, str]:
    from .cierres import comprobar_documento, PeriodoCerrado
    try:
        comprobar_documento(con, doc_id)
    except PeriodoCerrado as e:
        return False, str(e)
    """Aplica el circuito de aprobación. No se aprueba con incidencias críticas/altas abiertas."""
    from . import historial as _h
    _h.init(con)
    _h.guardar_version(con, doc_id, usuario, "antes de cambiar el estado")
    con.commit()
    if estado == "aprobada":
        faltan = requisitos_aprobacion(con, doc_id, rol)
        if faltan:
            return False, "No se puede aprobar todavía:\n- " + "\n- ".join(faltan)
        bloq = db.rows(con, "SELECT mensaje FROM incidencias WHERE documento_id=? AND resuelta=0 "
                            "AND severidad IN ('critica','alta')", (doc_id,))
        if bloq:
            return False, "No se puede aprobar: hay incidencias críticas/altas sin resolver:\n- " + \
                "\n- ".join(b["mensaje"] for b in bloq)
    campos = {"revisada": ("revisado_por", "revisado_en"), "aprobada": ("aprobado_por", "aprobado_en")}
    if estado in ("aprobada", "revisada"):
        try:
            from . import plantillas
            plantillas.init(con)
            plantillas.aprender(con, doc_id)        # la próxima factura de este proveedor se lee con lo aprendido
        except Exception:
            pass
    with db.tx(con):
        con.execute("UPDATE documentos SET estado=? WHERE id=?", (estado, doc_id))
        if estado in campos:
            a, b = campos[estado]
            con.execute(f"UPDATE documentos SET {a}=?, {b}=? WHERE id=?", (usuario, db.now_iso(), doc_id))
        db.audit(con, usuario, f"estado:{estado}", "documento", doc_id, {"comentario": comentario})
    # Una factura aprobada se convierte automáticamente en conocimiento reutilizable.
    # Si Ollama/embeddings no están disponibles, no bloqueamos nunca la aprobación.
    if estado == "aprobada":
        try:
            from . import knowledge_base as kb
            from .lector import config as lector_config
            cfg = lector_config(con)
            kb.index_document(con, doc_id, cfg["ollama_url"], cfg["ollama_embedding_modelo"])
        except Exception:
            pass
    return True, "Estado actualizado"


def reabrir_incidencia(con, inc_id: int, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE incidencias SET resuelta=0, resuelta_por=NULL, resuelta_en=NULL WHERE id=?", (inc_id,))
        db.audit(con, usuario, "reabrir_incidencia", "incidencia", inc_id, None)


def resolver_incidencia(con, inc_id: int, usuario: str, comentario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE incidencias SET resuelta=1, resuelta_por=?, resuelta_en=?, comentario=? WHERE id=?",
                    (usuario, db.now_iso(), comentario, inc_id))
        db.audit(con, usuario, "resolver_incidencia", "incidencia", inc_id, {"comentario": comentario})


def _dec_str(v):
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return None
    s = format(D(v), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def alta_manual(con, datos: dict, usuario: str) -> int:
    """Factura sin PDF (o con PDF escaneado ilegible) registrada a mano. Pasa por los mismos controles."""
    import hashlib
    from .money import q2
    clave = f"manual|{datos.get('emisor_nif')}|{datos.get('numero')}|{db.now_iso()}"
    h = hashlib.sha256(clave.encode()).hexdigest()
    base = q2(D(datos["base"]))
    tipo_iva = D(datos.get("tipo_iva") or 0)
    cuota = q2(base * tipo_iva / 100)
    ret_pct = D(datos.get("ret_pct") or 0)
    ret = q2(base * ret_pct / 100)
    irpf_pct = D(datos.get("irpf_pct") or 0)
    irpf = q2(base * irpf_pct / 100)
    total = base + cuota
    with db.tx(con):
        cur = con.execute("INSERT INTO documentos (file_hash, filename, file_path, paginas, tiene_texto, estado) VALUES (?,?,?,?,?,?)",
                          (h, f"(manual) {datos.get('emisor_nombre')} {datos.get('numero')}", "", 0, 0, "pendiente_revision"))
        did = cur.lastrowid
    res = {"modelo": "alta manual", "data": {
        "tipo_documento": datos.get("tipo", "factura"),
        "emisor": {"nombre": datos.get("emisor_nombre"), "nif": datos.get("emisor_nif"), "iban": datos.get("iban")},
        "receptor": {"nombre": "LLORCA GROUP HISPANIA, S.L.", "nif": datos.get("receptor_nif") or "B54727722"},
        "numero": datos.get("numero"), "fecha": datos.get("fecha"), "fecha_vencimiento": datos.get("vencimiento"),
        "obra": {"texto_literal": None, "codigo": None}, "concepto_general": datos.get("concepto"),
        "lineas": [{"descripcion": datos.get("concepto") or "Importe", "importe": str(base), "tipo_linea": "normal",
                    "partida_codigo": datos.get("partida_codigo"), "partida_confianza": 1.0}],
        "base_imponible": str(base), "impuestos": [{"tipo_pct": str(tipo_iva), "base": str(base), "cuota": str(cuota)}],
        "inversion_sujeto_pasivo": bool(datos.get("isp")), "exencion_motivo": datos.get("exencion"),
        "total_factura": str(total),
        "retencion_garantia": {"pct": str(ret_pct), "base": str(base), "importe": str(ret)} if ret else {},
        "irpf": {"pct": str(irpf_pct), "importe": str(irpf)} if irpf else {},
        "total_a_pagar": str(total - ret - irpf), "confianza": 1.0, "campos_dudosos": []}}
    aplicar_extraccion(con, did, res, usuario, obra_defecto=datos.get("obra_id"))
    con.execute("UPDATE documentos SET obra_id=?, obra_confianza=1 WHERE id=?", (datos.get("obra_id"), did))
    con.commit()
    validar_y_guardar(con, did)
    return did


def aprobar_sin_incidencias(con, usuario: str, obra_id: int | None = None, rol: str | None = None) -> int:
    """Aprobación masiva SOLO de documentos pendientes/revisados sin ninguna incidencia abierta (salvo 'info')."""
    sql = """SELECT d.id FROM documentos d WHERE d.estado IN ('pendiente_revision','revisada')
             AND NOT EXISTS (SELECT 1 FROM incidencias i WHERE i.documento_id=d.id AND i.resuelta=0 AND i.severidad<>'info')"""
    p = []
    if obra_id:
        sql += " AND d.obra_id=?"; p.append(obra_id)
    ids = [r["id"] for r in db.rows(con, sql, p)]
    n = 0
    for i in ids:
        ok, _ = cambiar_estado(con, i, "aprobada", usuario, "aprobación masiva: sin incidencias", rol=rol)
        n += ok
    return n
