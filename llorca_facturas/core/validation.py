"""
Motor de validación determinista. Aquí NO interviene la IA: todo es aritmética
exacta (Decimal) y reglas explícitas. Cada regla tiene un código estable para
poder filtrarla, resolverla y auditarla.

Tolerancias (justificación matemática):
- Cuadres de totales: ±0,02 €. Dos importes impresos redondeados a céntimo
  pueden diferir hasta 0,005 € cada uno respecto al valor real; al encadenar
  sumas (base + IVA + recargo) el error máximo teórico es 0,015 € -> 0,02 €.
- Cuota de IVA por tipo: ±0,01 €. La cuota se calcula como round(base × tipo)
  por cada tipo impositivo (art. 10 del RD 1619/2012 permite redondeo por
  tipo). Si el emisor redondea línea a línea, el error puede crecer hasta
  n_lineas × 0,005 €: en ese caso se informa como 'info', no como error.
- Línea (cantidad × precio × (1 − dto)): el precio unitario impreso con p
  decimales tiene un error de hasta 0,5·10^-p por unidad, luego el error de la
  línea es ≤ |cantidad| · 0,5·10^-p + 0,005 €.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from . import db
from .config import EMPRESA_NIF, TOL_CUADRE_CENTS, TOL_IVA_CENTS, TIPOS_COMPUTABLES
from .fiscal import normalize_nif, validate_nif, validate_iban, normalize_iban, mask_iban
from .money import D, q2, from_cents, fmt_eur, amount_variants, ZERO, CENT
from .pdf_utils import normalize_for_search

TOL = Decimal(TOL_CUADRE_CENTS) / 100
TOL_IVA = Decimal(TOL_IVA_CENTS) / 100


@dataclass
class Incidencia:
    codigo: str
    severidad: str       # critica | alta | media | info
    mensaje: str
    detalle: str = ""


def _dec_places(s: str | None) -> int:
    if not s or "." not in str(s):
        return 0
    return len(str(s).split(".")[1])


def normalizar_numero_factura(numero: str | None) -> str:
    """'FA-0012005704' ~ 'FA12005704' ; '90-26' ~ '9026' ; quita ceros a la izquierda de bloques."""
    if not numero:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", str(numero)).upper()
    return re.sub(r"(?<![0-9])0+(?=\d)", "", s)


def sociedades(con) -> dict:
    """Sociedades del grupo a cuyo nombre pueden venir las facturas: {NIF: nombre}."""
    raw = db.get_setting(con, "sociedades", "")
    socs = {}
    if raw:
        try:
            socs = {normalize_nif(k): v for k, v in json.loads(raw).items()}
        except Exception:
            socs = {}
    if not socs:
        socs = {normalize_nif(db.get_setting(con, "empresa_nif", EMPRESA_NIF)): "LLORCA GROUP HISPANIA, S.L."}
    return socs


def _norm_soc(t: str) -> str:
    t = re.sub(r"[^A-Z0-9 ]", " ", (t or "").upper())
    return re.sub(r"\b(S ?L ?U?|S ?A ?U?|SOCIEDAD|LIMITADA|ANONIMA|UNIPERSONAL)\b", " ", t).strip()


def cargar_modelo(con, doc_id: int) -> dict:
    doc = db.one(con, "SELECT * FROM documentos WHERE id=?", (doc_id,))
    if not doc:
        raise ValueError(f"Documento {doc_id} no existe")
    doc["lineas"] = db.rows(con, "SELECT * FROM lineas WHERE documento_id=? ORDER BY orden, id", (doc_id,))
    doc["impuestos"] = db.rows(con, "SELECT * FROM impuestos WHERE documento_id=? ORDER BY id", (doc_id,))
    return doc


def _c(v):
    return from_cents(v) if v is not None else None


def validar(con, doc: dict) -> list[Incidencia]:
    inc: list[Incidencia] = []
    add = lambda *a: inc.append(Incidencia(*a))  # noqa: E731

    tipo = doc.get("tipo_documento") or "factura"
    computable = tipo in TIPOS_COMPUTABLES
    base = _c(doc.get("base_imponible_cents"))
    bruto = _c(doc.get("importe_bruto_cents"))
    total_fact = _c(doc.get("total_factura_cents"))
    total_pagar = _c(doc.get("total_a_pagar_cents"))
    iva = sum((from_cents(t["cuota_cents"]) for t in doc["impuestos"]), ZERO)
    recargo = sum((from_cents(t["recargo_cents"] or 0) for t in doc["impuestos"]), ZERO)
    irpf = _c(doc.get("irpf_cents")) or ZERO
    ret = _c(doc.get("ret_garantia_cents")) or ZERO
    lineas = doc["lineas"]

    if not computable:
        add("C21", "info", f"Documento de tipo «{tipo}»: no computa como coste de obra",
            "Se conserva como documento soporte (certificación, albarán, parte de horas, proforma…).")

    # ---------------------------------------------------------------- identificación
    if not doc.get("numero"):
        add("C20", "alta", "Número de factura no identificado", "")
    nif_e = normalize_nif(doc.get("emisor_nif"))
    if not nif_e:
        add("C09", "alta", "NIF del emisor no identificado", "Sin NIF no se puede contabilizar ni detectar duplicados con fiabilidad.")
    elif re.match(r"^[A-Z]{2}[0-9A-Z]{8,12}$", nif_e) and not re.match(r"^[A-Z]\d", nif_e) and nif_e[:2] != "ES":
        pass                                         # NIF-IVA extranjero: se valida por formato, no con el algoritmo español
    else:
        ok, msg = validate_nif(nif_e)
        if not ok:
            add("C09", "alta", f"NIF del emisor no válido: {nif_e}", msg)
    if (nif_e and nif_e == normalize_nif(db.get_setting(con, "empresa_nif", EMPRESA_NIF))) or \
            "LLORCA" in (doc.get("emisor_nombre") or "").upper():
        add("C23", "critica", "El emisor coincide con la propia empresa",
            "Se ha tomado a LLORCA como proveedor. Corrija el emisor en la cabecera (quien factura, no el cliente).")
    nif_r = normalize_nif(doc.get("receptor_nif"))
    empresa = normalize_nif(db.get_setting(con, "empresa_nif", EMPRESA_NIF))
    socs = sociedades(con)
    if computable:
        if not nif_r:
            add("C10", "alta", "NIF del receptor no identificado", "Comprobar que la factura está a nombre de la empresa.")
        elif nif_r not in socs:
            add("C10", "critica", f"La factura NO está a nombre de ninguna sociedad del grupo (receptor {nif_r})",
                f"Sociedades configuradas: {', '.join(f'{v} ({k})' for k, v in socs.items())}. No es deducible ni contabilizable.")
        elif doc.get("receptor_nombre"):
            from rapidfuzz import fuzz
            nombre_ok = socs[nif_r]
            if nombre_ok and fuzz.token_set_ratio(_norm_soc(doc["receptor_nombre"]), _norm_soc(nombre_ok)) < 70:
                otra = next((f"{v} ({k})" for k, v in socs.items() if k != nif_r and
                             fuzz.token_set_ratio(_norm_soc(doc["receptor_nombre"]), _norm_soc(v)) >= 70), None)
                add("C29", "alta", f"La factura va a nombre de «{doc['receptor_nombre']}» pero con el NIF de {nombre_ok} ({nif_r})",
                    ("Parece de otra sociedad del grupo: " + otra + ". " if otra else "") +
                    "Compruebe a qué sociedad debe contabilizarse y, si es un error del proveedor, pida rectificación.")

    # ---------------------------------------------------------------- fechas
    f = doc.get("fecha")
    fd = None
    if not f:
        add("C13", "alta", "Fecha de factura no identificada", "")
    else:
        try:
            fd = datetime.strptime(f, "%Y-%m-%d").date()
            if fd > date.today() + timedelta(days=1):
                add("C13", "alta", f"Fecha de factura en el futuro ({fd:%d/%m/%Y})", "")
            if fd < date.today() - timedelta(days=5 * 365):
                add("C13", "media", f"Fecha de factura muy antigua ({fd:%d/%m/%Y})", "")
        except ValueError:
            add("C13", "alta", f"Fecha con formato no válido: {f}", "")
    if doc.get("fecha_vencimiento") and fd:
        try:
            fv = datetime.strptime(doc["fecha_vencimiento"], "%Y-%m-%d").date()
            if fv < fd:
                add("C13", "media", "El vencimiento es anterior a la fecha de factura", f"{fv:%d/%m/%Y} < {fd:%d/%m/%Y}")
        except ValueError:
            pass

    # ---------------------------------------------------------------- documento equivocado / obra de la subida
    texto_doc = doc.get("texto") or ""
    if re.search(r"%OR\b", texto_doc) and re.search(r"\bAN\b", texto_doc) and re.search(r"\bAC\b", texto_doc):
        add("C25", "critica", "Parece una CERTIFICACIÓN A CLIENTE, no una factura de proveedor",
            "Impórtela en Certificaciones. Este documento no debe contar como coste.")
    if doc.get("base_imponible_cents") is None and not nif_e and doc.get("estado") != "sin_procesar":
        add("C25", "alta", "No parece una factura: no se encuentran importes ni NIF del emisor",
            "¿Es el archivo correcto? Si no lo es, envíelo a la papelera.")
    if doc.get("obra_lote") and doc.get("obra_id") and doc["obra_id"] != doc["obra_lote"] and not doc.get("obra_forzada"):
        o1 = db.one(con, "SELECT codigo FROM obras WHERE id=?", (doc["obra_id"],))
        o2 = db.one(con, "SELECT codigo FROM obras WHERE id=?", (doc["obra_lote"],))
        if o1 and o2:
            add("C24", "alta", f"El documento indica la obra {o1['codigo']} pero se subió en el lote de la obra {o2['codigo']}",
                "Se ha asignado la que indica el documento. Si fue un error al subirlo, cámbiela en la cabecera.")
    menc = [o["codigo"] for o in db.rows(con, "SELECT codigo FROM obras")
            if re.search(rf"(?<![\d.,/]){re.escape(o['codigo'])}(?![\d.,/])", texto_doc)]
    if len(menc) > 1:
        add("C24", "alta", f"El documento menciona varias obras: {', '.join(menc)}",
            "Compruebe a qué obra corresponde o si hay que repartir las líneas.")

    # ---------------------------------------------------------------- obra
    if computable:
        if not doc.get("obra_id"):
            add("C14", "alta", "Obra no identificada", f"Referencia en el documento: «{doc.get('referencia_obra_texto') or '—'}»")
        elif (doc.get("obra_confianza") or 0) < 0.8:
            add("C14", "media", f"Obra asignada con confianza baja ({(doc.get('obra_confianza') or 0):.0%})",
                f"Referencia: «{doc.get('referencia_obra_texto') or '—'}»")

    # ---------------------------------------------------------------- líneas
    suma = sum((from_cents(l["importe_cents"]) for l in lineas), ZERO)
    if base is None:
        add("C01", "critica", "Base imponible no identificada", "")
    elif lineas:
        diff = suma - base
        if abs(diff) <= TOL:
            pass
        elif bruto is not None and abs(suma - bruto) <= TOL:
            add("C01", "media", f"Las líneas suman el bruto ({fmt_eur(bruto)}); entre bruto y base hay {fmt_eur(base - bruto, sign=True)} sin desglosar",
                "Probable descuento o deducción de anticipo global. En el análisis por partida se imputa como ajuste.")
        else:
            add("C01", "alta", f"Las líneas no suman la base imponible (descuadre {fmt_eur(diff, sign=True)})",
                f"Σ líneas = {fmt_eur(suma)} · Base = {fmt_eur(base)}. Revisa líneas omitidas, duplicadas o mal leídas.")
    elif computable:
        add("C01", "media", "Documento sin líneas de detalle", "No se podrá repartir el coste por partidas.")

    for l in lineas:
        cant, precio = l.get("cantidad"), l.get("precio_unitario")
        if cant in (None, "") or precio in (None, ""):
            continue
        dto = D(l.get("descuento_pct") or 0)
        teor = D(cant) * D(precio) * (1 - dto / 100)
        err_max = abs(D(cant)) * Decimal("0.5") * Decimal(10) ** (-_dec_places(precio)) + Decimal("0.005")
        err_max = max(err_max, Decimal("0.02"))
        real = from_cents(l["importe_cents"])
        if abs(q2(teor) - real) > err_max and abs(q2(-teor) - real) > err_max:
            add("C15", "media", f"Línea {l.get('orden')}: cantidad × precio no coincide con el importe",
                f"«{(l.get('descripcion') or '')[:70]}» → {cant} × {precio}"
                f"{f' × (1−{dto}%)' if dto else ''} = {fmt_eur(teor)} ≠ {fmt_eur(real)} (tolerancia {fmt_eur(err_max)})")

    # ---------------------------------------------------------------- IVA
    if base is not None:
        n_lin = max(1, len(lineas))
        for t in doc["impuestos"]:
            tb, tc, tp = from_cents(t["base_cents"]), from_cents(t["cuota_cents"]), D(t["tipo_pct"])
            teor = q2(tb * tp / 100)
            d = tc - teor
            if abs(d) > TOL_IVA:
                if abs(d) <= Decimal("0.005") * n_lin:
                    add("C02", "info", f"IVA {tp}%: diferencia de redondeo {fmt_eur(d, sign=True)}",
                        f"{fmt_eur(tb)} × {tp}% = {fmt_eur(teor)}; factura: {fmt_eur(tc)}. Compatible con redondeo por línea.")
                else:
                    add("C02", "alta", f"Cuota de IVA {tp}% incorrecta ({fmt_eur(d, sign=True)})",
                        f"{fmt_eur(tb)} × {tp}% = {fmt_eur(teor)} y la factura dice {fmt_eur(tc)}.")
            if t.get("recargo_pct"):
                teor_r = q2(tb * D(t["recargo_pct"]) / 100)
                if abs(from_cents(t["recargo_cents"] or 0) - teor_r) > TOL_IVA:
                    add("C02", "alta", f"Recargo de equivalencia {t['recargo_pct']}% incorrecto",
                        f"Esperado {fmt_eur(teor_r)}; factura {fmt_eur(from_cents(t['recargo_cents'] or 0))}")
        if doc["impuestos"]:
            sb = sum((from_cents(t["base_cents"]) for t in doc["impuestos"]), ZERO)
            if abs(sb - base) > TOL:
                add("C03", "alta", "La suma de bases por tipo de IVA no coincide con la base imponible",
                    f"Σ bases IVA = {fmt_eur(sb)} · Base = {fmt_eur(base)}")

        isp = bool(doc.get("inversion_sujeto_pasivo"))
        if isp and iva != 0:
            add("C08", "alta", "Indica inversión del sujeto pasivo pero repercute IVA",
                f"Con ISP (art. 84.Uno.2º.f LIVA) la cuota debe ser 0; la factura repercute {fmt_eur(iva)}.")
        if computable and iva == 0 and base != 0 and not isp and not doc.get("exencion_motivo"):
            add("C08", "alta", "Factura sin IVA y sin causa de exención o ISP indicada",
                "Una factura sin IVA debe mencionar la exención o la inversión del sujeto pasivo.")
        if computable and isp and ret == 0 and tipo == "factura":
            add("C22", "info", "Subcontrata (ISP) sin retención de garantía",
                "Comprobar si el contrato con el industrial prevé retención de garantía.")

        # ------------------------------------------------------------ totales
        if total_fact is not None:
            teor = base + iva + recargo
            if abs(teor - total_fact) > TOL:
                add("C04", "alta", f"Base + IVA + recargo ≠ total factura ({fmt_eur(total_fact - teor, sign=True)})",
                    f"{fmt_eur(base)} + {fmt_eur(iva)} + {fmt_eur(recargo)} = {fmt_eur(teor)}; total impreso {fmt_eur(total_fact)}")
        if doc.get("ret_garantia_cents"):
            pct = D(doc.get("ret_garantia_pct") or 0)
            rbase = _c(doc.get("ret_garantia_base_cents")) or base
            if pct:
                teor = q2(rbase * pct / 100)
                if abs(teor - ret) > TOL:
                    add("C05", "alta", f"Retención de garantía mal calculada ({fmt_eur(ret - teor, sign=True)})",
                        f"{fmt_eur(rbase)} × {pct}% = {fmt_eur(teor)}; factura {fmt_eur(ret)}")
            if rbase != base and abs(rbase - base) > TOL:
                add("C05", "media", "La retención de garantía se calcula sobre una base distinta de la base imponible",
                    f"Base retención {fmt_eur(rbase)} · Base imponible {fmt_eur(base)}")
        if doc.get("irpf_cents"):
            pct = D(doc.get("irpf_pct") or 0)
            if pct:
                teor = q2(base * pct / 100)
                if abs(teor - irpf) > TOL:
                    add("C06", "alta", f"Retención IRPF mal calculada ({fmt_eur(irpf - teor, sign=True)})",
                        f"{fmt_eur(base)} × {pct}% = {fmt_eur(teor)}; factura {fmt_eur(irpf)}")
        if total_pagar is not None:
            ref = total_fact if total_fact is not None else base + iva + recargo
            teor = ref - irpf - ret
            if abs(teor - total_pagar) > TOL:
                add("C07", "alta", f"El líquido a pagar no cuadra ({fmt_eur(total_pagar - teor, sign=True)})",
                    f"{fmt_eur(ref)} − IRPF {fmt_eur(irpf)} − garantía {fmt_eur(ret)} = {fmt_eur(teor)}; "
                    f"factura: {fmt_eur(total_pagar)}")

        # ------------------------------------------------------------ signo
        if tipo == "abono" and base > 0:
            add("C17", "media", "Abono con importe positivo", "Un abono/rectificativa debe restar coste (importes negativos).")
        if tipo in ("factura", "anticipo") and base < 0:
            add("C17", "media", "Factura con base negativa", "¿Es realmente un abono? Revisa el tipo de documento.")

    # ---------------------------------------------------------------- anclaje al PDF
    texto = doc.get("texto") or ""
    if doc.get("tiene_texto") and texto:
        t = normalize_for_search(texto)
        no_encontrados = []
        for nombre, v in (("base imponible", base), ("total factura", total_fact), ("total a pagar", total_pagar)):
            if v is None or v == 0:
                continue
            if not any(var in t for var in amount_variants(v)):
                no_encontrados.append(f"{nombre} {fmt_eur(v)}")
        if no_encontrados:
            add("C16", "media", "Importes no localizados literalmente en el PDF",
                "No aparecen en la capa de texto: " + "; ".join(no_encontrados) +
                ". Puede ser un error de lectura de la IA o un formato raro: verificar a la vista del PDF.")
    elif computable:
        add("C16", "info", "PDF escaneado (sin capa de texto)",
            "Los importes no se pueden anclar al texto: la revisión humana a la vista del PDF es obligatoria.")

    # ---------------------------------------------------------------- IBAN
    iban = normalize_iban(doc.get("iban"))
    if iban:
        ok, msg = validate_iban(iban)
        if not ok:
            add("C11", "critica", f"IBAN no válido ({mask_iban(iban)})", msg + ". No pagar hasta verificar.")
        elif doc.get("proveedor_id"):
            otros = db.rows(con, "SELECT iban, primera_vez FROM proveedor_ibans WHERE proveedor_id=? AND iban<>?",
                            (doc["proveedor_id"], iban))
            verif = db.one(con, "SELECT verificado FROM proveedor_ibans WHERE proveedor_id=? AND iban=?",
                           (doc["proveedor_id"], iban))
            if otros and not (verif and verif["verificado"]):
                primero = db.one(con, "SELECT MIN(primera_vez) AS p FROM proveedor_ibans WHERE proveedor_id=? AND iban=?",
                                 (doc["proveedor_id"], iban))
                previo_min = min((o["primera_vez"] or "9999") for o in otros)
                if (primero and primero["p"] or "") > previo_min:
                    add("C11", "critica", "CAMBIO DE CUENTA BANCARIA del proveedor",
                        f"Esta factura pide el pago en {mask_iban(iban)} y antes se usaba "
                        f"{', '.join(mask_iban(o['iban']) for o in otros)}. Confirmar por teléfono con el proveedor "
                        "(número conocido, no el de la factura) antes de pagar: patrón típico de fraude.")

    # ---------------------------------------------------------------- duplicados
    num_n = doc.get("numero_normalizado")
    if nif_e and num_n:
        dups = db.rows(con, "SELECT id, filename, numero FROM documentos WHERE emisor_nif=? AND numero_normalizado=? "
                            "AND id<>? AND tipo_documento=? AND estado NOT IN ('rechazada','eliminado','duplicado')", (nif_e, num_n, doc["id"], tipo))
        if dups:
            add("C12", "critica", f"POSIBLE DUPLICADO: mismo proveedor y nº de factura ({doc.get('numero')})",
                "Coincide con: " + "; ".join(f"#{d['id']} {d['filename']}" for d in dups))
    if nif_e and total_pagar is not None and doc.get("fecha") and computable:
        sim = db.rows(con, "SELECT id, filename, numero FROM documentos WHERE emisor_nif=? AND total_a_pagar_cents=? "
                           "AND fecha=? AND id<>? AND COALESCE(numero_normalizado,'')<>? AND estado NOT IN ('rechazada','eliminado','duplicado')",
                      (nif_e, doc["total_a_pagar_cents"], doc["fecha"], doc["id"], num_n or ""))
        if sim:
            add("C12", "alta", "Posible duplicado: mismo proveedor, fecha e importe con distinto número",
                "Coincide con: " + "; ".join(f"#{d['id']} nº {d['numero']} ({d['filename']})" for d in sim))

    # ---------------------------------------------------------------- albarán facturado dos veces
    if computable and nif_e:
        albs = {_norm_alb(l.get("albaran")) for l in lineas if l.get("albaran")}
        try:
            albs |= {_norm_alb(a) for a in json.loads(doc.get("albaranes") or "[]") if a}
        except Exception:
            pass
        albs.discard("")
        if albs:
            otros = db.rows(con, """SELECT DISTINCT d.id, d.numero, d.filename, l.albaran FROM lineas l JOIN documentos d ON d.id=l.documento_id
                                    WHERE d.emisor_nif=? AND d.id<>? AND d.estado NOT IN ('rechazada','eliminado','duplicado')
                                    AND d.tipo_documento IN ('factura','anticipo') AND l.albaran IS NOT NULL""", (nif_e, doc["id"]))
            repes = {}
            for o in otros:
                a = _norm_alb(o["albaran"])
                if a in albs:
                    repes.setdefault(a, set()).add(f"#{o['id']} nº {o['numero']}")
            for a, docs_ in repes.items():
                add("C26", "critica", f"Albarán {a} ya facturado en otra factura del mismo proveedor",
                    "Aparece también en " + ", ".join(sorted(docs_)) + ". Posible doble cobro: compruebe antes de pagar.")

    # ---------------------------------------------------------------- precios fuera de lo habitual
    if computable and nif_e and lineas:
        hist = db.rows(con, """SELECT l.descripcion, l.precio_unitario FROM lineas l JOIN documentos d ON d.id=l.documento_id
                               WHERE d.emisor_nif=? AND d.id<>? AND d.estado NOT IN ('rechazada','eliminado','duplicado')
                               AND l.precio_unitario IS NOT NULL""", (nif_e, doc["id"]))
        por_art = {}
        for h in hist:
            por_art.setdefault(_clave_art(h["descripcion"]), []).append(D(h["precio_unitario"]))
        avisos_p = []
        for l in lineas:
            if not l.get("precio_unitario") or not l.get("descripcion"):
                continue
            prev = [p for p in por_art.get(_clave_art(l["descripcion"]), []) if p > 0]
            if len(prev) >= 2:
                prev.sort()
                med = prev[len(prev) // 2]
                pu = D(l["precio_unitario"])
                exceso = (pu - med) * abs(D(l.get("cantidad") or 1))
                if med > 0 and pu > med * Decimal("1.10") and exceso >= 20:
                    avisos_p.append(f"«{l['descripcion'][:40]}»: {pu} frente a {med} habitual (+{(pu / med - 1) * 100:.0f} %, "
                                    f"{fmt_eur(exceso)} de más)")
        if avisos_p:
            add("C27", "media", f"{len(avisos_p)} línea(s) con precio superior al habitual de este proveedor",
                "; ".join(avisos_p[:5]) + ". Compare con la oferta o el contrato.")

    # ---------------------------------------------------------------- IVA según el tipo de operación
    if computable and base and base > 0 and doc["impuestos"]:
        txt_l = " ".join((l.get("descripcion") or "").lower() for l in lineas)
        mano_obra = bool(re.search(r"mano de obra|\bhoras?\b|trabajos? (de|realizados)|instalaci[oó]n|montaje|colocaci[oó]n|ejecuci[oó]n|"
                                   r"certificaci[oó]n|administraci[oó]n|alicatado|solado|pintado|enlucido|tabiquer", txt_l))
        materiales = bool(re.search(r"\bkg\b|saco|palet|\bud\b|suministro|material|mortero|cemento|ladrillo|tubo|cable|pintura "
                                    r"plástica|rollo|bote", txt_l))
        isp = bool(doc.get("inversion_sujeto_pasivo"))
        if not isp and iva > 0 and mano_obra and not materiales:
            add("C28", "media", "Trabajos de obra facturados con IVA: ¿debería ser inversión del sujeto pasivo?",
                "Las ejecuciones de obra de subcontratistas para una promoción suelen llevar ISP (art. 84.Uno.2º.f LIVA). "
                "Confírmelo con la asesoría antes de contabilizar.")
        if isp and materiales and not mano_obra:
            add("C28", "media", "Suministro de materiales con inversión del sujeto pasivo: ¿es correcto?",
                "La ISP de la construcción se aplica a ejecuciones de obra, no a la mera entrega de materiales.")

    # ---------------------------------------------------------------- certificación/albarán de proveedor sin factura
    if tipo in ("certificacion", "albaran") and base and base > 0 and not re.search(r"%OR\b", doc.get("texto") or ""):
        from .obra_control import devengado_pendiente
        if any(x["id"] == doc["id"] for x in devengado_pendiente(con, doc.get("obra_id"))):
            add("C32", "info", f"{'Certificación' if tipo == 'certificacion' else 'Albarán'} de proveedor aún sin factura",
                "Es coste ya ejecutado pendiente de facturar: cuenta como provisión en el cierre del mes hasta que llegue la factura.")

    # ---------------------------------------------------------------- moneda, idioma y proveedores extranjeros
    if (doc.get("moneda") or "EUR") != "EUR":
        add("C33", "alta", f"Factura en {doc['moneda']}, no en euros",
            "Los importes no son euros: hay que convertirlos al tipo de cambio de la fecha de la factura antes de contabilizar.")
    if nif_e and re.match(r"^(AT|BE|BG|CY|CZ|DE|DK|EE|EL|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK)", nif_e):
        add("C34", "info", "Proveedor de otro país de la UE: adquisición intracomunitaria",
            "El proveedor no repercute IVA español; la empresa lo autoliquida (IVA soportado y repercutido). Confírmelo con la asesoría.")
        if iva > 0:
            add("C34", "media", "Proveedor comunitario que repercute IVA", "Una factura intracomunitaria no debería llevar IVA español.")
    if doc.get("idioma") and doc["idioma"] not in ("es", "ca", "desconocido"):
        add("C35", "info", f"Documento en otro idioma ({doc['idioma']})", "Revise con especial atención los campos leídos.")

    # ---------------------------------------------------------------- rectificativas y periodo
    if tipo == "abono" and not doc.get("factura_rectificada"):
        add("C30", "media", "Factura rectificativa sin referencia a la factura que corrige",
            "Una rectificativa debe indicar la factura original. Solicítela al proveedor.")
    elif doc.get("factura_rectificada") and nif_e:
        orig = db.one(con, "SELECT id FROM documentos WHERE emisor_nif=? AND numero_normalizado=?",
                      (nif_e, normalizar_numero_factura(doc["factura_rectificada"])))
        if not orig:
            add("C30", "info", f"No está registrada la factura original {doc['factura_rectificada']}", "")
    if fd and doc.get("creado_en"):
        try:
            alta = datetime.fromisoformat(str(doc["creado_en"]).replace(" ", "T")[:19]).date()
            if (alta - fd).days > 45 and (alta.year, alta.month) != (fd.year, fd.month):
                add("C31", "info", f"Factura de {fd:%m/%Y} registrada el {alta:%d/%m/%Y}",
                    "Llega fuera del mes de su fecha: revise el periodo de contabilización y, si ya se cerró el mes, la provisión.")
        except Exception:
            pass

    # ---------------------------------------------------------------- calidad IA
    conf = doc.get("confianza")
    if conf is not None and conf < 0.8:
        add("C18", "media", f"Confianza de la lectura baja ({conf:.0%})", doc.get("observaciones_ia") or "")
    dudosos = json.loads(doc.get("campos_dudosos") or "[]")
    if dudosos:
        add("C18", "info", "Campos marcados como dudosos por la IA", ", ".join(dudosos))
    if doc.get("observaciones_ia"):
        add("C18", "info", "Observación de la IA", doc["observaciones_ia"])

    sin_partida = [l for l in lineas if not l.get("partida_id")]
    if computable and sin_partida:
        add("C19", "info", f"{len(sin_partida)} línea(s) sin partida asignada", "Asígnalas en la revisión para el control de costes.")
    return inc


def validar_y_guardar(con, doc_id: int) -> list[Incidencia]:
    """Revalida y sustituye las incidencias abiertas (respeta las ya resueltas si siguen existiendo)."""
    doc = cargar_modelo(con, doc_id)
    nuevas = validar(con, doc)
    resueltas = {(r["codigo"], r["mensaje"]): r for r in
                 db.rows(con, "SELECT * FROM incidencias WHERE documento_id=? AND resuelta=1", (doc_id,))}
    with db.tx(con):
        con.execute("DELETE FROM incidencias WHERE documento_id=?", (doc_id,))
        for i in nuevas:
            prev = resueltas.get((i.codigo, i.mensaje))
            con.execute(
                "INSERT INTO incidencias (documento_id, codigo, severidad, mensaje, detalle, resuelta, resuelta_por, resuelta_en, comentario) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (doc_id, i.codigo, i.severidad, i.mensaje, i.detalle,
                 1 if prev else 0, prev and prev["resuelta_por"], prev and prev["resuelta_en"], prev and prev["comentario"]))
    return nuevas


def _norm_alb(a) -> str:
    a = re.sub(r"\(.*?\)", "", str(a or ""))
    return re.sub(r"[^A-Z0-9]", "", a.upper())


def _clave_art(desc) -> str:
    return re.sub(r"[^a-z0-9]", "", (desc or "").lower())[:24]


def resumen_cuadre(doc: dict) -> list[dict]:
    """Tabla de cuadre paso a paso para mostrar en la revisión."""
    base = _c(doc.get("base_imponible_cents")) or ZERO
    iva = sum((from_cents(t["cuota_cents"]) for t in doc["impuestos"]), ZERO)
    rec = sum((from_cents(t["recargo_cents"] or 0) for t in doc["impuestos"]), ZERO)
    irpf = _c(doc.get("irpf_cents")) or ZERO
    ret = _c(doc.get("ret_garantia_cents")) or ZERO
    suma = sum((from_cents(l["importe_cents"]) for l in doc["lineas"]), ZERO)
    tf = _c(doc.get("total_factura_cents"))
    tp = _c(doc.get("total_a_pagar_cents"))

    def fila(concepto, calculado, impreso):
        ok = None if impreso is None else abs(calculado - impreso) <= TOL
        return {"Concepto": concepto, "Calculado": fmt_eur(calculado),
                "Impreso": "—" if impreso is None else fmt_eur(impreso),
                "Diferencia": "—" if impreso is None else fmt_eur(impreso - calculado, sign=True),
                "Cuadra": "—" if ok is None else ("Sí" if ok else "No")}

    out = [fila("Σ líneas vs base imponible", suma, base)]
    out.append(fila("Base + IVA + recargo = total factura", base + iva + rec, tf))
    out.append(fila("Total − IRPF − garantía = a pagar", (tf if tf is not None else base + iva + rec) - irpf - ret, tp))
    return out
