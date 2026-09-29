"""
Lectura LOCAL de facturas: nada sale del ordenador.

Tres piezas, todas en la máquina del usuario:
1. TEXTO:  capa de texto del PDF (pdfplumber). Si es escaneado -> OCR en CPU con RapidOCR
           (modelos ONNX de ~15 MB incluidos en el paquete pip; sin instalar nada más).
2. MOTOR DE REGLAS (sin modelo de IA): NIF/CIF e IBAN con su dígito de control, fechas, número de
   factura y, sobre todo, un SOLVER MATEMÁTICO: entre todos los importes del documento busca la
   combinación que cumple  base × tipo = cuota,  base + cuota = total,  total − retención = líquido.
   Una factura casi nunca contiene por casualidad tres números que cumplan esas ecuaciones, así que
   cuando cierran, la lectura es fiable. Las líneas de detalle se aceptan solo si
   cantidad × precio × (1 − dto) = importe.
3. IA LOCAL opcional (Ollama, p.ej. qwen2.5:7b): mismo esquema JSON que el resto de la app.
   Si está disponible se ejecutan AMBOS motores y se contrastan: si coinciden en base y total,
   la confianza sube; si discrepan, se marca para revisión humana.
"""
from __future__ import annotations

import io
import json
import re
import time
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from itertools import combinations

from .fiscal import validate_nif, normalize_nif, validate_iban, normalize_iban
from .money import parse_amount, q2
from .pdf_utils import read_text, has_text_layer

EMPRESA_NIFS = {"B54727722"}

# ============================================================================ 1. TEXTO
_OCR = None


def _ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def ocr_pdf(data: bytes, max_paginas: int = 6, escala: float = 2.2) -> str:
    """OCR en CPU. Reconstruye las líneas agrupando las cajas por altura."""
    import numpy as np
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    paginas = []
    for i in range(min(len(pdf), max_paginas)):
        img = pdf[i].render(scale=escala).to_pil()
        res, _ = _ocr_engine()(np.array(img))
        if not res:
            continue
        cajas = []
        for box, txt, conf in res:
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            cajas.append(((min(ys) + max(ys)) / 2, min(xs), max(ys) - min(ys), txt))
        cajas.sort()
        lineas, actual, y_ref = [], [], None
        for y, x, h, t in cajas:
            if y_ref is None or abs(y - y_ref) <= max(8, h * 0.55):
                actual.append((x, t))
                y_ref = y if y_ref is None else (y_ref + y) / 2
            else:
                lineas.append("   ".join(t for _, t in sorted(actual)))
                actual, y_ref = [(x, t)], y
        if actual:
            lineas.append("   ".join(t for _, t in sorted(actual)))
        paginas.append("\n".join(lineas))
    return "\n\f".join(paginas)


def obtener_texto(data: bytes, usar_ocr: bool = True) -> tuple[str, str]:
    """Devuelve (texto, método) con método 'texto' u 'ocr'."""
    texto, _ = read_text(data)
    if has_text_layer(texto):
        # también la versión con disposición: conserva columnas en las tablas
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                lay = "\n".join((p.extract_text(layout=True) or "") for p in pdf.pages[:10])
            return lay if len(lay) > len(texto) * 0.5 else texto, "texto"
        except Exception:
            return texto, "texto"
    if not usar_ocr:
        return texto, "sin_texto"
    return ocr_pdf(data), "ocr"


# ============================================================================ 2. REGLAS
RE_IMPORTE = re.compile(r"(?<![\w/.,-])-?\s?(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2}|\.\d{2})(?!\d)|(?<![\w/.,])\d{1,3}(?:,\d{3})+\.\d{2}(?!\d)")
RE_NIF = re.compile(r"\b(?:ES[\s-]?)?([A-HJNP-SUVW][\s.-]?\d{2}[\s.-]?\d{3}[\s.-]?\d{3}|\d{1,2}\.?\d{3}\.?\d{3}[\s-]?[A-Z]|[XYZ][\s.-]?\d{7}[\s-]?[A-Z])\b")
RE_IBAN = re.compile(r"ES\d{2}(?:[\s-]?\d{4}){5}(?!\d)")   # sin \b: el OCR pega «BBVAES92…»
MESES = {m: i for i, m in enumerate(["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"], 1)}
MESES_LARGOS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
                "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}

ETQ = {
    "base": r"base\s*imponible|b\.\s*imponible|base\s*i\.?v\.?a|importe\s*base|base\b|subtotal|sub-total|total\s*neto|neto|"
            r"importe\s*neto|importe\s*mes|resultado|base\s*imposable|net\s*amount|taxable|suma\s*importes|total\s*sin\s*iva|"
            r"total\s*base",
    "iva": r"i\.?\s?v\.?\s?a|cuota|impuestos?|igic|vat|tax\b|iva\s*repercutido",
    "total": r"total\s*factura|importe\s*total|total\s*\(eur\)|total\s*eur|total\s*€|total\b|importe\s*neto|total\s*documento|"
             r"total\s*con\s*iva|grand\s*total|amount\s*due|total\s*invoice",
    "pagar": r"a\s*pagar|l[ií]quido|a\s*percibir|a\s*cobrar|saldo\s*adeudado|total\s*a\s*pagar|resultado|importe\s*a\s*pagar|"
             r"total\s*l[ií]quido|neto\s*a\s*pagar|a\s*abonar|pendiente",
    "ret": r"retenci[oó]n|garant[ií]a|irpf|fianza|ret\.",
}


def compactar(texto: str) -> str:
    """'1 .892,36' -> '1.892,36' ; '9 4,62' -> '94,62'. Solo espacios SIMPLES (las columnas van separadas por varios)."""
    t = re.sub(r"(?<=\d) (?=[.,]\d)|(?<=[.,]) (?=\d)", "", texto)
    return re.sub(r"(?<=\d) (?=\d)", "", t)


# «artículo 84,1.2º,f», «art. 84.Uno», «Ley 37/1992»: números de normas, no importes
RE_REF_LEGAL = re.compile(r"(art[íi]culos?|arts?\.?|apartado|ley|decreto|rd|directiva|disposici[oó]n)\s*(n[º°o]\.?\s*)?$", re.I)


def _importes(texto: str) -> list[dict]:
    """Todos los importes con su contexto (lo que hay a su izquierda en la línea y la línea anterior)."""
    out = []
    lineas = texto.splitlines()
    for i, ln in enumerate(lineas):
        for m in RE_IMPORTE.finditer(ln):
            raw = m.group(0).replace(" ", "") if re.search(r"\d \d{3}", m.group(0)) else m.group(0).strip()
            v = parse_amount(raw)
            if abs(v) > Decimal("50000000"):
                continue
            izq = ln[:m.start()][-70:].lower()
            if RE_REF_LEGAL.search(izq.rstrip()) or re.match(r"[.,]\d+\s*[º°oª]?\s*[,.]", ln[m.end():]):
                continue
            prev = lineas[i - 1].lower() if i else ""
            out.append({"v": q2(v), "linea": i, "izq": izq, "prev": prev, "pos": m.start(), "texto_linea": ln})
    return out


ETQ_FUERTE = {"base": r"base\s*imponible|b\.\s*imponible|base\s*imposable|taxable\s*amount",
              "total": r"total\s*factura|total\s*certificaci[oó]n|importe\s*total|total\s*a\s*pagar|total\s*documento|total\s*fra"}
ETQ_DEBIL = r"subtotal|sub-total|suma\s*importes|importe\s*mes|resultado|total\s*bruto|importe\s*bruto|bruto"


def _etiqueta(it: dict, clave: str) -> float:
    """Fuerza de la etiqueta que acompaña a un importe: 1,5 si es inequívoca («Base imponible», «Total factura»),
    1 si es propia de ese campo, 0,5 si es ambigua («Subtotal», «Bruto»)."""
    fuerte = ETQ_FUERTE.get(clave)
    if fuerte and re.search(rf"({fuerte})[^0-9]{{0,40}}$", it["izq"]):
        return 1.5
    if clave == "base" and re.search(rf"({ETQ_DEBIL})[^0-9]{{0,30}}$", it["izq"]):
        return 0.5
    pat = ETQ[clave]
    if re.search(rf"({pat})[^0-9]{{0,40}}$", it["izq"]):
        return 1.0
    if re.search(pat, it["izq"]):
        return 0.6
    if re.search(pat, it["prev"]):
        return 0.4   # etiqueta en la cabecera de la columna / línea de arriba
    return 0.0


def _resolver_totales(imps: list[dict], texto: str) -> dict:
    """SOLVER: busca (base, tipo, cuota, total) que cierran matemáticamente, con retención y líquido."""
    tl = texto.lower()
    es_abono = bool(re.search(r"abono|rectificativa|devoluci", tl))
    if es_abono and not getattr(_resolver_totales, "_neg", False):
        max_neg = max((-it["v"] for it in imps if it["v"] < 0), default=Decimal(0))
        max_pos = max((it["v"] for it in imps if it["v"] > 0), default=Decimal(0))
        if max_neg > 0 and max_neg >= max_pos:
            r = _resolver_negativo(imps, texto)
            if r:
                return r
    isp = bool(re.search(r"sujeto\s*pasivo|84\.?\s*uno|art[íi]culo\s*84|art\.?\s*84", tl))
    exento = bool(re.search(r"exent", tl))
    valores = defaultdict(list)
    for it in imps:
        if it["v"] > 0:
            valores[it["v"]].append(it)
    vals = sorted(valores)
    absv = {abs(it["v"]) for it in imps}
    top = sorted(vals, reverse=True)[:3]
    maximo = top[0] if top else Decimal(1)

    def magnitud(t, b):
        """Los totales suelen estar entre los mayores importes; una base diminuta frente al máximo es sospechosa."""
        sc = 0.0
        if t in top:
            sc += 1.5 - 0.4 * top.index(t)
        if maximo and b < maximo * Decimal("0.02"):
            sc -= 2.0
        return sc          # las retenciones suelen imprimirse en negativo
    cand = []

    def puntua(v, clave):
        return max((_etiqueta(it, clave) for it in valores.get(v, [])), default=0)

    # a) con IVA
    for b in vals:
        for tipo in (Decimal(21), Decimal(10), Decimal(4), Decimal(5), Decimal(7), Decimal(3), Decimal("9.5"), Decimal(15)):
            c = q2(b * tipo / 100)
            for dc in (Decimal("0"), Decimal("0.01"), Decimal("-0.01")):
                cc = c + dc
                t = b + cc
                if cc in valores and t in valores and cc != b:
                    s = 3 + puntua(b, "base") + puntua(cc, "iva") + puntua(t, "total") + puntua(t, "pagar") * 0.5 - float(abs(dc)) * 50
                    s += min(len(valores[b]), 3) * 0.1 + magnitud(t, b)
                    cand.append({"base": b, "tipo": tipo, "cuota": cc, "total": t, "score": s})
    # b) sin IVA (ISP / exenta): base = total. El criterio fuerte es que CIERRE la retención:
    #    base × p% = r  y  base − r = líquido, con los tres importes presentes en el documento.
    def cierre_ret(b):
        for p in (Decimal(5), Decimal(10), Decimal(15), Decimal(7), Decimal(19)):
            r = q2(b * p / 100)
            for rr in (r, r + Decimal("0.01"), r - Decimal("0.01")):
                if rr in absv and (b - rr) in valores and rr != b:
                    return 3.5
        return 0.0
    if isp or exento or not cand:
        for b in vals:
            sb, st_, cr = puntua(b, "base"), puntua(b, "total"), cierre_ret(b)
            if sb or st_ or cr:
                cand.append({"base": b, "tipo": Decimal(0), "cuota": Decimal(0), "total": b,
                             "score": 1 + sb + st_ + cr + (1.5 if (isp or exento) else 0) + min(len(valores[b]), 2) * 0.1
                                      + magnitud(b, b)})
        if not cand and maximo and (isp or exento):
            # sin etiquetas legibles (maquetación rota): en ISP/exenta la base es el mayor importe impreso
            cand.append({"base": maximo, "tipo": Decimal(0), "cuota": Decimal(0), "total": maximo, "score": 1.0})
    ceros = [it for it in imps if it["v"] == 0]
    if any(_etiqueta(it, "base") >= 0.6 for it in ceros) and any(_etiqueta(it, "total") >= 0.6 for it in ceros):
        cand.append({"base": Decimal("0.00"), "tipo": Decimal(21) if re.search(r"iva\s*21", tl) else Decimal(0),
                     "cuota": Decimal("0.00"), "total": Decimal("0.00"), "score": 7.0})
    if not cand:
        # abonos: todos los importes en negativo -> se resuelve con el signo invertido
        if es_abono and not getattr(_resolver_totales, "_neg", False):
            return _resolver_negativo(imps, texto)
        return {}
    mejor = max(cand, key=lambda x: (x["score"], x["base"]))
    b, t = mejor["base"], mejor["total"]
    # retenciones: r = base × p ; líquido = total − Σ r
    rets = []
    for p in (Decimal(5), Decimal(10), Decimal(15), Decimal(7), Decimal(19), Decimal(2), Decimal(3)):
        r = q2(b * p / 100)
        for rr in (r, r + Decimal("0.01"), r - Decimal("0.01")):
            if rr in absv:
                ctx = " ".join(it["izq"] + " " + it["prev"] for it in valores.get(rr, []) + [x for x in imps if x["v"] == -rr])
                tipo_r = "irpf" if re.search(r"irpf", ctx) or p in (7, 15, 19) else "garantia"
                if re.search(ETQ["ret"], ctx) or (t - rr) in valores:
                    rets.append({"pct": p, "importe": rr, "tipo": tipo_r, "cierra": (t - rr) in valores})
                break
    base_impresa = [it["v"] for it in imps if _etiqueta(it, "base") == 1.0 and re.search(r"base\s*imponible", it["izq"])]
    mejor["base_impresa_distinta"] = next((v for v in base_impresa if v != b and v > 0), None)
    ret_g = next((r for r in sorted(rets, key=lambda r: -r["cierra"]) if r["tipo"] == "garantia"), None)
    irpf = next((r for r in sorted(rets, key=lambda r: -r["cierra"]) if r["tipo"] == "irpf"), None)
    liquido = t - (ret_g["importe"] if ret_g else 0) - (irpf["importe"] if irpf else 0)
    return {**mejor, "isp": isp and mejor["cuota"] == 0, "exento": exento and not isp and mejor["cuota"] == 0,
            "ret_g": ret_g, "irpf": irpf, "liquido": liquido, "liquido_en_doc": liquido in valores,
            "cierra_iva": mejor["tipo"] > 0, "n_candidatos": len(cand)}


def _resolver_negativo(imps: list[dict], texto: str) -> dict:
    """Abonos: resuelve con los importes negativos en positivo y devuelve el resultado con el signo invertido."""
    negs = [dict(it, v=-it["v"]) for it in imps if it["v"] < 0]
    if not negs:
        return {}
    _resolver_totales._neg = True
    try:
        r = _resolver_totales(negs, texto)
    finally:
        _resolver_totales._neg = False
    if r:
        for k in ("base", "cuota", "total", "liquido"):
            r[k] = -r[k]
        for k in ("ret_g", "irpf"):
            if r.get(k):
                r[k] = dict(r[k], importe=-r[k]["importe"])
        if r.get("base_impresa_distinta"):
            r["base_impresa_distinta"] = -r["base_impresa_distinta"]
        r["abono"] = True
    return r


def _nums_linea(ln: str):
    out = []
    for m in re.finditer(r"(?<![\w.,/:-])-?(?:\d{1,3}(?:\.\d{3})*,\d{1,4}|\d+(?:[.,]\d{1,4})?)(?![\w/])", ln):
        n = m.group(0)
        v = parse_amount(n) if "," in n else Decimal(n.replace(",", "."))
        if abs(v) < Decimal("1e8"):
            out.append((m.start(), v))
    return out


def _cierra(vals, imp):
    """Busca cantidad × precio × (1 − dto/100) = importe entre los números de la fila."""
    cand = vals[:-1]
    dtos = [Decimal(0)] + [v for _, v in cand if 0 < v <= 100]
    for (pa, a), (pb, b) in combinations(cand, 2):
        if a == 0 or b == 0:
            continue
        for d in dtos:
            if abs(q2(a * b * (1 - d / 100)) - imp) <= Decimal("0.02"):
                # orden de columnas: cantidad a la izquierda del precio
                return (a, b, d, min(pa, pb)) if pa <= pb else (b, a, d, min(pa, pb))
    return None


UNIDADES_LINEA = {"ud": "ud", "uds": "ud", "u": "ud", "un": "ud", "und": "ud", "unid": "ud", "pza": "ud", "pz": "ud",
                  "m": "m", "ml": "m", "mts": "m", "m2": "m2", "m²": "m2", "m3": "m3", "m³": "m3", "kg": "kg", "kgs": "kg",
                  "t": "t", "tn": "t", "l": "l", "lt": "l", "h": "h", "hr": "h", "hora": "h", "horas": "h", "pa": "pa",
                  "saco": "saco", "sacos": "saco", "rollo": "rollo", "caja": "caja", "palet": "palet", "jornada": "jornada"}
RE_ALBARAN = re.compile(r"(?:alb(?:ar[aá]n)?\.?\s*(?:n[º°o]\.?)?\s*[:#]?\s*)?\b(\d{1,4}\s*/\s*[\d.]{3,}|[A-Z]{1,4}[/-]?\d{3,})\b"
                        r".{0,20}?fecha\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I)


def _tipo_linea(desc: str) -> str:
    d = desc.lower()
    if re.search(r"\bporte|transporte|desplazamiento|env[ií]o\b", d):
        return "portes"
    if re.search(r"\bpalet|envase|embalaje|big bag vac", d):
        return "portes"
    if re.search(r"descuento|dto\.? pronto|bonificaci", d):
        return "descuento"
    if re.search(r"anticipo|entrega a cuenta|a deducir", d):
        return "anticipo_deducido"
    return "normal"


def _unidad(ln_antes: str, desc: str = "", cantidad=None) -> tuple[str | None, bool]:
    """Unidad: 1) columna propia antes de la cantidad («TUBO CORRUGADO   m   25,00»), separada por 2+ espacios
    (nunca «25KG» pegado al artículo); 2) si no hay columna, «ud» para artículos envasados/por pieza.
    Devuelve (unidad, es_columna)."""
    m = re.search(r"\s{2,}([A-Za-z²³]{1,6}\.?)\s*$", ln_antes)
    if m and m.group(1).lower().strip(".") in UNIDADES_LINEA:
        return UNIDADES_LINEA[m.group(1).lower().strip(".")], True
    d = desc.lower()
    entera = cantidad is not None and Decimal(str(cantidad)) == Decimal(str(cantidad)).to_integral_value()
    # unidad escrita como palabra suelta en la descripción («… 30MM ML», «… M2», «(M3)»)
    if re.search(r"(?<![\w,.])(ml|m\.l\.|mts\.? lineales)(?![\w])", d) and not re.search(r"\d\s*ml\b", d):
        return "m", False
    if re.search(r"(?<![\w,.(])m2(?![\w])", d) and not re.search(r"\(\s*[\d.,]+\s*m2\s*\)", d):
        return "m2", False
    if re.search(r"(?<![\w,.(])m3(?![\w])", d):
        return "m3", False
    # cantidades con decimales: superficie en revestimientos, peso en acero
    if not entera and cantidad is not None:
        if re.search(r"azulejo|porcel|gres|\bess\b|solado|plaqueta|pavimento|revestimiento|\d+[,.]?\d*\s?x\s?\d+", d):
            return "m2", False
        if re.search(r"corrugado|ferralla|acero|redondo|hierro", d):
            return "kg", False
        if re.search(r"tubo|tuberia|cable|perfil|canal|junta|cordon", d):
            return "m", False
        if re.search(r"horas?|mano de obra|oficial|peon", d):
            return "h", False
    if re.search(r"\bhoras?\b|mano de obra|\boficial\b|\bpe[oó]n\b", d):
        return "h", False
    if entera:
        return "ud", False          # cantidades enteras sin otra indicación: piezas / unidades
    return None, False


def _lineas_detalle(texto: str) -> list[dict]:
    """Filas de tabla que CIERRAN: cantidad × precio × (1 − dto%) = importe (±0,02).
    Admite el descuento como columna sin «%», el precio desplazado a la línea siguiente y
    agrupaciones por albarán («10 / 29.188  Fecha: 02/07/2026»)."""
    out = []
    lineas = texto.splitlines()
    albaran_actual = None
    for i, ln in enumerate(lineas):
        ma = RE_ALBARAN.search(ln)
        if ma:
            albaran_actual = f"{re.sub(r'\\s+', '', ma.group(1))} ({ma.group(2)})"
        vals = _nums_linea(ln)
        if len(vals) < 2:
            continue
        imp_pos, imp = vals[-1]
        if imp == 0 or "suma y sigue" in ln.lower() or \
                re.match(r"\s*(total|base imponible|b\.\s*imponible|subtotal|bruto|i\.?v\.?a|retenci|importe total)\b", ln.lower()):
            continue
        hallado = _cierra(vals, imp) if len(vals) >= 3 else None
        if not hallado:
            for sig in lineas[i + 1:i + 3]:
                sv = _nums_linea(sig)
                if len(sv) == 1 and not re.search(r"[A-Za-z]{3,}", sig):
                    hallado = _cierra(vals[:-1] + [(imp_pos - 1, sv[0][1])] + [vals[-1]], imp)
                    if hallado:
                        break
        if hallado:
            cant, precio, d, pos = hallado
            antes = ln[:pos]
            desc = re.sub(r"^\s*[\w.-]*\d[\w.-]*\s+(?=[A-Za-zÁÉÍÓÚÑ*])", "", antes).strip(" -|:*")
            desc = re.sub(r"\s{2,}", " ", desc)
            if len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", desc)) < 3:
                # «50%  4  Instalación electrocerraduras …  144,20 €»: la descripción va entre las cifras
                tramos = re.split(r"(?<!\w)-?\d[\d.,]*\s*[€%]?(?!\w)", ln)
                mejor = max(tramos, key=lambda x: len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", x)))
                desc = re.sub(r"\s{2,}", " ", mejor).strip(" -|:*€%")
            ud, en_columna = _unidad(antes, desc, cant)
            if en_columna:
                desc = re.sub(r"\s+[A-Za-z²³]+\.?$", "", desc).strip()
            if len(desc) >= 3:
                out.append({"descripcion": desc[:160], "cantidad": str(cant), "unidad": ud, "precio_unitario": str(precio),
                            "descuento_pct": str(d) if d else None, "importe": str(q2(imp)),
                            "tipo_linea": _tipo_linea(desc), "albaran": albaran_actual})
    return out


def _lineas_concepto(texto: str, base: Decimal) -> list[dict]:
    """Facturas de servicios: filas con texto y un único importe al final. Se aceptan SOLO si un bloque contiguo
    de ellas suma exactamente la base (así no se cuelan teléfonos, totales ni referencias)."""
    cand = []
    for ln in texto.splitlines():
        low = ln.lower()
        if re.match(r"\s*(total|base|b\.\s*imponible|subtotal|bruto|i\.?v\.?a|retenci|importe total|suma|a pagar|l[ií]quido|"
                    r"vencimiento|iban|forma de pago|cuota)", low) or "suma y sigue" in low:
            cand.append(None)
            continue
        m = re.search(r"(-?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\s*€?\s*$", ln.rstrip())
        txt = ln[:m.start()] if m else ""
        if m and len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{3,}", txt)) >= 2 and \
                not re.search(r"(?i)gracias|importe|total|subtotal|base|i\.?v\.?a|pagar|saldo|\bmes\b|p[aá]gina|hoja|tel[eé]fono", txt):
            cand.append({"descripcion": re.sub(r"\s{2,}", " ", txt).strip(" -|:*")[:160], "importe": str(q2(parse_amount(m.group(1))))})
        else:
            cand.append(None)
    filas = [c for c in cand if c]
    n = len(filas)
    for i in range(n):
        suma = Decimal(0)
        for j in range(i, min(n, i + 80)):
            suma += Decimal(filas[j]["importe"])
            if abs(suma - base) <= Decimal("0.02") and j >= i:
                bloque = filas[i:j + 1]
                if len(bloque) >= 1 and not (len(bloque) == 1 and Decimal(bloque[0]["importe"]) == base and
                                             re.search(r"(?i)total|base", bloque[0]["descripcion"])):
                    return [dict(b, tipo_linea=_tipo_linea(b["descripcion"])) for b in bloque]
    return []


def _puentes(texto: str, faltan: Decimal, base: Decimal) -> list[dict]:
    """Conceptos de pie de factura que completan la base (servicios, palets, portes, envases…)."""
    etiquetas = r"serv|palet|porte|envase|embalaje|transporte|gastos|recargo|ecotasa|punto verde|seguro"
    lineas = texto.splitlines()
    for i, ln in enumerate(lineas):
        if not re.search(etiquetas, ln, re.I) or not re.search(r"b\.?\s*imponible|base", ln, re.I):
            continue
        cab = ln
        for sig in lineas[i + 1:i + 3]:
            vals = _nums_linea(sig)
            if not vals:
                continue
            nums = [(p, v) for p, v in vals if 0 < v < base]
            for k in (1, 2, 3):
                for combo in combinations(nums, k):
                    if abs(sum(v for _, v in combo) - faltan) <= Decimal("0.02"):
                        res = []
                        for p, v in combo:
                            m = None
                            for mm in re.finditer(r"[A-Za-zÁÉÍÓÚÑ.]{3,}", cab):
                                if mm.start() <= p + 12:
                                    m = mm
                            res.append({"descripcion": (m.group(0).strip(".").capitalize() if m else "Otros conceptos") +
                                        " (pie de factura)", "importe": str(q2(v)), "tipo_linea": "portes"})
                        return res
    return []


def _valor_columna(texto: str, etiqueta: str, patron_valor: str, lineas_max: int = 4) -> str | None:
    """Tablas de cabecera: «FACTURA Nº  FECHA FACTURA  CLIENTE» y los valores en la fila de abajo, alineados por columna."""
    lineas = texto.splitlines()
    for i, ln in enumerate(lineas):
        m = re.search(etiqueta, ln, re.I)
        if not m:
            continue
        c0, c1 = m.start(), m.end()
        vistas = 0
        for sig in lineas[i + 1:i + 1 + lineas_max * 2]:
            if not sig.strip():
                continue
            vistas += 1
            centro = (c0 + c1) / 2
            cands = [t for t in re.finditer(patron_valor, sig) if t.start() <= c1 + 10 and t.end() >= c0 - 10]
            if cands:
                # el valor más centrado bajo la etiqueta (evita tomar la fecha de la columna vecina)
                return min(cands, key=lambda t: abs((t.start() + t.end()) / 2 - centro)).group(0)
            if vistas >= lineas_max:
                break
    return None


def _parse_fecha(v: str) -> date | None:
    m = re.match(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", v or "")
    if not m:
        return None
    d_, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y + 2000 if y < 100 else y, mo, d_)
    except ValueError:
        return None


def _fechas(texto: str) -> list[tuple[int, date, str]]:
    out = []
    hoy = date.today()
    for m in re.finditer(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", texto):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            out.append((m.start(), date(y, mo, d), texto[max(0, m.start() - 40):m.start()].lower()))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})[\s-]([a-z]{3})[a-z]*[\s-](\d{2,4})\b", texto.lower()):
        mo = MESES.get(m.group(2))
        if mo:
            y = int(m.group(3)); y = y + 2000 if y < 100 else y
            try:
                out.append((m.start(), date(y, mo, int(m.group(1))), texto[max(0, m.start() - 40):m.start()].lower()))
            except ValueError:
                pass
    for m in re.finditer(r"\b(\d{1,2}) de ([a-z]+) de (\d{4})", texto.lower()):
        mo = MESES_LARGOS.get(m.group(2))
        if mo:
            try:
                out.append((m.start(), date(int(m.group(3)), mo, int(m.group(1))), texto[max(0, m.start() - 40):m.start()].lower()))
            except ValueError:
                pass
    for m in re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", texto):
        try:
            out.append((m.start(), date(int(m.group(1)), int(m.group(2)), int(m.group(3))), texto[max(0, m.start() - 40):m.start()].lower()))
        except ValueError:
            pass
    for m in re.finditer(r"\b([a-z]+)\s+(\d{1,2}),?\s+(20\d{2})\b", texto.lower()):
        mo = MESES_LARGOS.get(m.group(1)) or MESES.get(m.group(1)[:3])
        if mo:
            try:
                out.append((m.start(), date(int(m.group(3)), mo, int(m.group(2))), texto[max(0, m.start() - 40):m.start()].lower()))
            except ValueError:
                pass
    out.sort(key=lambda x: x[0])
    return [x for x in out if date(2015, 1, 1) <= x[1] <= date(hoy.year + 1, 12, 31)]


ETQ_NUM = (r"(factura\s*n[º°o]?\.?|n[º°o]\.?\s*(?:de\s*)?factura|n[úu]mero|serie\s*factura|n[º°]\s*fra\.?|invoice\s*n[oº°]?|"
           r"n[º°o]\.?\s*(?:de\s*)?documento|documento\s*n[º°o]|n[úu]m\.?\s*(?:de\s*)?(?:factura|doc)|ref\.?\s*factura|"
           r"n[º°o]\s*de\s*serie|factura\s*simplificada|n[úu]mero\s*de\s*factura|codi\s*factura|n[úu]mero\s*factura)")


def _numero_factura(texto: str, filename: str = "") -> str | None:
    def valido(v):
        return v and re.search(r"\d", v) and not re.fullmatch(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", v) and len(v) >= 2 \
            and not validate_nif(v)[0]
    # 1) etiqueta y valor en la misma línea
    for m in re.finditer(r"(?:n[º°o]\.?\s*(?:de\s*)?factura|factura\s*n[º°o]\.?|n[úu]mero\s*(?:de\s*factura)?|factura[\s.]*:|"
                         r"invoice\s*(?:n[oº°]\.?|number|#)?|n\.º de factura|fra\.?\s*n?[º°]?|n[º°o]\.?\s*(?:de\s*)?documento|"
                         r"documento\s*n[º°o]\.?|n[úu]m\.\s*(?:factura|doc\.?))[\s:.#]*([A-Z0-9][A-Z0-9/\-_.]{1,20}(?:\s*/\s*\d+)?)",
                         texto, re.I):
        antes = texto[max(0, m.start() - 25):m.start()].lower()
        if re.search(r"(\bde\s+la|\bdel?|\bs/|seg[uú]n|sobre|\ba\s+la|abono|rectifica\w*)\s*$", antes):
            continue                          # factura referenciada, no la del documento
        v = m.group(1).strip(".-")
        if valido(v):
            return re.sub(r"\s+", "", v)
    # 2) cabecera de tabla: la etiqueta arriba y el valor debajo, en la misma columna
    # 2a) «FACTURA 1 / 171», «Factura FP260300»: serie y número junto a la palabra factura
    m = re.search(r"\bFactura\s+(\d{1,4}\s*/\s*\d{1,8}|[A-Z]{1,4}\d{3,}[\w/-]*)\b", texto, re.I)
    if m and valido(m.group(1)):
        return re.sub(r"\s+", "", m.group(1))
    v = _valor_columna(texto, ETQ_NUM + r"(?![A-Za-zÁÉÍÓÚáéíóú0-9])", r"[A-Z0-9][A-Z0-9/\-_.]*\d[A-Z0-9/\-_.]*")
    if v and valido(v) and not re.fullmatch(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", v):
        return v.strip(".-")
    # 3) «Factura 1 / 197», «Factura FP260300»
    m = re.search(r"n[º°o]\.?\s*(?:de\s*)?factura\s*[:.]?\s*([A-Z]{1,4}\s*[/-]\s*\d{2,})", texto, re.I)
    if m:
        return re.sub(r"\s+", "", m.group(1))
    m = re.search(r"\bFactura\s+(\d+\s*/\s*\d+|[A-Z]{0,4}\d[\w/-]{2,})", texto, re.I)
    if m and valido(m.group(1)):
        return re.sub(r"\s+", "", m.group(1))
    m = re.search(r"factura[\s\S]{0,120}?\b([A-Z]{1,4}-?\d{2,}[/-]\d{2,4})\b", texto, re.I)
    if m:
        return m.group(1)
    # 4) el nombre del archivo suele llevar el número: se acepta solo si aparece literalmente en el documento
    for tok in sorted(re.findall(r"[A-Z]{0,5}-?\d[\w-]{1,15}", filename.upper()), key=len, reverse=True):
        tok = tok.strip("-_")
        if len(tok) >= 3 and valido(tok) and re.search(rf"(?<![\w]){re.escape(tok)}(?![\w])", texto.upper()) \
                and not re.fullmatch(r"20\d{2}", tok):
            return tok
    return None


def _nombre_emisor(texto: str, nif: str | None, proveedores: dict) -> str | None:
    if nif and nif in proveedores and "LLORCA" not in (proveedores[nif] or "").upper():
        return proveedores[nif]            # aprende: si ya se conoce el NIF, se usa su nombre
    cand = []
    RUIDO_NOMBRE = {"FACTURA", "CERTIFICACION", "CERTIFICACIÓN", "PROFORMA", "ALBARAN", "ALBARÁN", "ABONO", "PRESUPUESTO"}
    for ln in texto.splitlines()[:60]:
        s = re.sub(r"\s{2,}", "  ", ln).strip()
        trozos = [x.strip() for x in s.split("  ") if x.strip()]
        for k, t in enumerate(trozos):
            if re.search(r"\b(S\.?\s?L\.?\s?U?\.?|S\.?\s?A\.?\s?U?\.?|SLU|SL|SA|S\.COOP|C\.B\.)\s*$", t, re.I) and "LLORCA" not in t.upper() \
                    and 2 < len(t) < 70:
                # nombres con letras espaciadas: «SISTEMA  DE  PLACAS  SEMPERE  S.L.U.», «DOMUS  SPAIN 2017 SL»
                j = k
                while j > 0 and re.fullmatch(r"[A-ZÁÉÍÓÚÑ0-9&.,' -]{1,40}", trozos[j - 1]) and trozos[j - 1].upper() not in RUIDO_NOMBRE:
                    j -= 1
                nombre = " ".join(trozos[j:k + 1])
                if len(re.sub(r"[^A-Za-zÁÉÍÓÚÑ]", "", nombre.replace("SLU", "").replace("SL", ""))) >= 3:
                    cand.append(nombre)
    if cand:
        return cand[0]
    if nif:
        dig = nif[1:-1] if nif[0].isalpha() else nif[:-1]
        for ln in texto.splitlines():
            plano = re.sub(r"[\s.-]", "", ln)
            if dig in plano:
                antes = re.split(r"(?i)\b(c\.?\s?i\.?\s?f|n\.?\s?i\.?\s?f|nif|cif|dni)\b|" + re.escape(nif[:1]) + r"[\s.-]?" + re.escape(dig[:2]),
                                 ln)[0]
                antes = re.split(r"\s{3,}|\||:", antes.strip())[-1].strip(" ,.:-")
                if re.search(r"[A-Za-zÁÉÍÓÚÑ]{3,}", antes) and "LLORCA" not in antes.upper() and 4 < len(antes) < 70 \
                        and not re.search(r"(?i)inscrita|registro|tomo|folio|calle|avda|c/", antes):
                    return antes
        lineas = texto.splitlines()
        for i, ln in enumerate(lineas):
            if nif[-4:-1] in re.sub(r"\D", "", ln) and re.search(r"nif|cif|dni", ln, re.I):
                for prev in reversed(lineas[max(0, i - 6):i]):
                    for trozo in re.split(r"\s{3,}", prev.strip()):
                        t = trozo.strip()
                        if re.fullmatch(r"[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ.' -]{5,60}", t) and len(t.split()) >= 2 \
                                and not re.search(r"LLORCA|CALLE|AVDA|AVENIDA|PLAZA|C/|FACTURA|ALICANTE|BENIDORM", t):
                            return t.title()
    return None


def _nombre_receptor(texto: str) -> str | None:
    """Nombre tal y como figura el cliente en la factura (para detectar facturas a nombre de otra sociedad del grupo)."""
    m = re.search(r"(?:nombre|cliente|raz[oó]n\s*social|facturar\s*a)\s*:\s*([^\n|]{4,70})", texto, re.I)
    if m and re.search(r"(?i)llorca", m.group(1)):
        return re.split(r"\s{2,}", m.group(1).strip())[0].strip(" .,")
    for ln in texto.splitlines():
        for tro in re.split(r"\s{3,}|\|", ln):
            t = tro.strip()
            if re.search(r"(?i)llorca", t) and 6 < len(t) < 70 and not re.search(r"(?i)factura|fra\.|obra", t):
                return re.sub(r"(?i)^(cliente|nombre)\s*:\s*", "", t).strip(" .,")
    return "LLORCA GROUP HISPANIA"


def _etiqueta_total_ok(imps, tot) -> bool:
    """ISP/exenta sin retención: vale si el importe aparece con etiqueta de base o de total."""
    return any(it["v"] == tot["base"] and (_etiqueta(it, "base") >= 0.6 or _etiqueta(it, "total") >= 0.6) for it in imps)


def motor_reglas(texto: str, partidas: list[dict] | None = None, proveedores: dict | None = None, filename: str = "",
                 plantillas: dict | None = None) -> dict:
    proveedores = proveedores or {}
    texto_c = compactar(texto)   # versión con números partidos reunidos: solo para candidatos de importe
    dudosos, obs = [], []
    # --- NIFs
    nifs = []
    for m in RE_NIF.finditer(texto.upper()):
        n = normalize_nif(m.group(0))
        if validate_nif(n)[0] and n not in nifs:
            nifs.append(n)
    receptor = next((n for n in nifs if n in EMPRESA_NIFS), None)
    emisores = [n for n in nifs if n not in EMPRESA_NIFS]
    emisor = next((n for n in emisores if n in proveedores), emisores[0] if emisores else None)
    if not emisor and proveedores:
        # aprende de lo ya validado: si el nombre de un proveedor conocido aparece en el documento, se toma su NIF
        from .maestros import strip_accents
        tn = re.sub(r"[^a-z0-9 ]", " ", strip_accents(texto[:4000]))
        for nif_p, nom in proveedores.items():
            pal = [w for w in re.sub(r"[^a-z0-9 ]", " ", strip_accents(nom or "")).split()
                   if len(w) > 3 and w not in ("s.l.", "slu", "sociedad", "limitada")][:2]
            if pal and all(re.search(rf"\b{re.escape(w)}\b", tn) for w in pal):
                emisor = nif_p
                obs.append(f"NIF no impreso o ilegible: se asigna {nif_p} por coincidencia con el proveedor conocido «{nom}».")
                dudosos.append("emisor.nif")
                break
    if not emisor:
        dudosos.append("emisor.nif")
    if len(emisores) > 1:
        obs.append(f"Varios NIF en el documento: {', '.join(emisores[:4])}")
    # --- IBAN
    ibans = [normalize_iban(m.group(0)) for m in RE_IBAN.finditer(texto.upper())]
    iban = next((i for i in ibans if validate_iban(i)[0]), None)
    # --- fechas
    fs = _fechas(texto)
    RF = r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"
    fecha = _parse_fecha(_valor_columna(texto, r"fecha\s*(de\s*)?factura|f\.\s*factura|fecha\s*emisi[oó]n|fecha\s*expedici[oó]n|"
                                             r"fecha\s*documento|f\.\s*emisi[oó]n|data\s*factura|invoice\s*date", RF))
    if not fecha:
        # la fecha de la factura, no la de los albaranes («Fecha: 02/07/2026» dentro del detalle)
        fecha = next((d for _, d, ctx in fs if re.search(r"(fecha|data)\s*(de\s*)?(factura|emisi|expedici|documento)|invoice\s*date", ctx)), None) or \
            next((d for _, d, ctx in fs if "fecha" in ctx and not re.search(r"venc|entrega|albar|pedido", ctx)
                  and not re.search(r"fecha:\s*$", ctx)), None) or \
            (fs[0][1] if fs else None)
    venc = next((d for _, d, ctx in fs if "venc" in ctx), None) or \
        _parse_fecha(_valor_columna(texto, r"vencimiento", RF))
    if not fecha:
        dudosos.append("fecha")
    numero = _numero_factura(texto, filename)
    aprendido = {}
    if plantillas and emisor and emisor in plantillas:
        from .plantillas import aplicar
        aprendido = aplicar(texto, plantillas[emisor])
        if aprendido.get("numero"):
            numero = aprendido["numero"].strip(".-")
            obs.append("Número leído con la plantilla aprendida de este proveedor.")
        if aprendido.get("fecha"):
            f_ = _parse_fecha(aprendido["fecha"]) or (date.fromisoformat(aprendido["fecha"]) if re.match(r"20\d{2}-", aprendido["fecha"]) else None)
            if f_:
                fecha = f_
    if not numero:
        dudosos.append("numero")
    # --- importes
    imps = _importes(texto)
    vistos = {(it["linea"], it["v"]) for it in imps}
    imps += [it for it in _importes(texto_c) if (it["linea"], it["v"]) not in vistos]
    tot = _resolver_totales(imps, texto)
    if aprendido.get("base_imponible") and (not tot or not tot.get("cierra_iva")):
        b_ = q2(parse_amount(aprendido["base_imponible"]))
        t_ = q2(parse_amount(aprendido.get("total_factura") or aprendido["base_imponible"]))
        if tot.get("base") != b_:
            cuota = t_ - b_
            tipo = next((tp for tp in (21, 10, 4, 5, 7, 0) if abs(q2(b_ * tp / 100) - cuota) <= Decimal("0.02")), None)
            if tipo is not None:
                tot = {"base": b_, "tipo": Decimal(tipo), "cuota": cuota, "total": t_, "isp": tipo == 0, "exento": False,
                       "ret_g": None, "irpf": None, "liquido": q2(parse_amount(aprendido.get("total_a_pagar") or str(t_))),
                       "liquido_en_doc": True, "cierra_iva": tipo > 0, "n_candidatos": 1}
                obs.append("Importes leídos con la plantilla aprendida de este proveedor.")
    if not tot:
        dudosos += ["base_imponible", "total_factura"]
    elif tot.get("base_impresa_distinta") is not None:
        dudosos.append("base_imponible")
        obs.append(f"El documento rotula como base imponible {tot['base_impresa_distinta']}, pero la base que cierra con la "
                   f"retención y el líquido es {tot['base']} (¿deducción de anticipo o entrega a cuenta?).")
    # --- líneas
    base = tot.get("base")
    lineas = []
    lin = []
    if base is not None:
        # de las dos lecturas (texto tal cual / números recompuestos) se queda la que más se acerca a la base
        variantes = [_lineas_detalle(v) for v in (texto, texto_c)]
        lin = min(variantes, key=lambda L_: abs(sum((Decimal(l["importe"]) for l in L_), Decimal(0)) - base))
    if lin and base is not None:
        s = sum((Decimal(l["importe"]) for l in lin), Decimal(0))
        if abs(s - base) <= Decimal("0.05"):
            lineas = lin
        else:
            extra = _puentes(texto, base - s, base) or _puentes(texto_c, base - s, base)
            s2 = s + sum((Decimal(x["importe"]) for x in extra), Decimal(0))
            if extra and abs(s2 - base) <= Decimal("0.05"):
                lineas = lin + extra
            elif base and s / base >= Decimal("0.5"):
                lineas = lin + extra + [{"descripcion": "Resto no desglosado (revisar en el PDF)", "importe": str(q2(base - s2)),
                                         "tipo_linea": "otro"}]
                dudosos.append("lineas")
                obs.append(f"{len(lin)} líneas leídas suman {s}; faltan {q2(base - s2)} hasta la base: se añade una línea de ajuste.")
            else:
                obs.append(f"Se detectaron {len(lin)} filas de detalle que suman {s} y no la base {base}: se registra una línea única.")
    if not lineas and base is not None and base != 0:
        lc = _lineas_concepto(texto, base)
        if lc and len(lc) >= 1:
            lineas = lc
            obs = [o for o in obs if "filas de detalle" not in o]
    if not lineas and base is not None:
        concepto = _concepto(texto)
        lineas = [{"descripcion": concepto, "importe": str(base)}]
        dudosos.append("lineas")
    # --- partida por palabras clave (sobre cada línea y, si no, sobre el documento)
    from .maestros import clasificar_linea
    for l in lineas:
        l.setdefault("tipo_linea", "normal")
        l.update({"partida_codigo": None, "partida_confianza": 0})
        if partidas:
            pid, conf = clasificar_linea(l["descripcion"] + " " + texto[:1500] if len(lineas) == 1 else l["descripcion"], partidas)
            if pid:
                l["partida_codigo"] = next(p["codigo"] for p in partidas if p["id"] == pid)
                l["partida_confianza"] = conf
    # --- forma de pago y pedido
    forma_pago = None
    mfp = re.search(r"(?:forma\s*de\s*pago|su forma de pago es|pagadera en|condiciones de pago)\s*[:.]?\s*([^\n]{3,60})", texto, re.I)
    if mfp:
        forma_pago = re.split(r"\s{3,}", mfp.group(1).strip())[0].strip(" :.")
        if len(forma_pago) < 3 or re.fullmatch(r"(vencimiento|importe)\b.*", forma_pago, re.I):
            sig = _valor_columna(texto, r"pagadera en|forma de pago", r"[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ ]{3,40}")
            forma_pago = sig.strip() if sig else None
    pedido = None
    mp = re.search(r"\bpedido\s*(?:n[º°o]\.?)?\s*[:#]?\s*([A-Z0-9][\w/ -]{2,20})", texto, re.I)
    if mp and re.search(r"\d", mp.group(1)):
        pedido = re.split(r"\s{2,}", mp.group(1).strip())[0]
    else:
        v = _valor_columna(texto, r"\bpedido\b", r"[A-Z0-9][\w/-]*\d[\w/-]*(?:\s\d+)?")
        pedido = v
    albs = sorted({l["albaran"] for l in lineas if l.get("albaran")})

    # --- tipo de documento
    tl = texto.lower()
    tipo = "factura"
    if (re.search(r"rectificativa|abono", tl) and base is not None and base < 0) or tot.get("abono"):
        tipo = "abono"
    elif re.search(r"\bproforma\b", tl):
        tipo = "proforma"
    elif re.search(r"^\W*certificaci[oó]n\b", "\n".join(x.strip() for x in texto.splitlines()[:12]).lower(), re.M) and \
            not re.search(r"\bfactura\b", tl[:1500]):
        tipo = "certificacion"
    elif re.search(r"anticipo|entrega a cuenta", tl) and not re.search(r"deducir|a deducir", tl):
        tipo = "anticipo"
    elif re.search(r"parte de (trabajo|horas)|partes de trabajo|total horas obra|total horas", tl) and \
            not re.search(r"\bfactura\b", tl[:800]) and not re.search(r"\bfactura\s*(n[º°o]|num|:)", tl):
        tipo = "parte_horas"
    elif re.search(r"\balbar[aá]n\b", tl) and not re.search(r"factura", tl):
        tipo = "albaran"
    # --- obra (texto literal cercano a 'obra', 'ref', 'asunto', 'proyecto')
    obra_txt = None
    for m in re.finditer(r"\b(?:obras?|ref(?:erencia)?|asunto|proyecto)\b\s*[:.]*\s*([^\n]{3,70})", texto, re.I):
        v = m.group(1).strip()
        if re.search(r"\d{3}|[A-Z]{4,}", v):
            obra_txt = v
            break
    m2 = re.search(r"\b(\d{3,4}\s*[-.]?\s*[A-Za-z][A-Za-z]{3,}[^\n]{0,40})", texto)
    if not obra_txt and m2:
        obra_txt = m2.group(1).strip()
    # --- confianza: suma de comprobaciones objetivas (cada una verificable), no una impresión
    nombre_em = _nombre_emisor(texto, emisor, proveedores)
    veces_base = sum(1 for it in imps if tot and it["v"] == tot.get("base"))
    mayor = max((it["v"] for it in imps), default=Decimal(0))
    cierra = bool(tot) and (bool(tot.get("cierra_iva")) or bool(tot.get("liquido_en_doc") and (tot.get("ret_g") or tot.get("irpf")))
                            or tot.get("base") == 0 or (tot.get("isp") and _etiqueta_total_ok(imps, tot))
                            # ISP sin etiquetas legibles: el mayor importe, repetido (base = total), en factura con mención a ISP
                            or (tot.get("isp") and tot.get("base") == mayor and veces_base >= 2))
    base_etq = bool(tot) and any(it["v"] == tot.get("base") and _etiqueta(it, "base") >= 1.0 for it in imps)
    chequeos = [
        (0.25, cierra),                                                   # los importes cumplen las ecuaciones fiscales
        (0.10, base_etq or (bool(tot) and tot.get("cierra_iva"))),         # la base lleva su etiqueta o cierra con el IVA
        (0.15, bool(emisor) and validate_nif(emisor)[0]),                  # CIF/NIF del emisor válido
        (0.05, bool(nombre_em)),
        (0.10, bool(numero)),
        (0.10, bool(fecha) and fecha <= date.today()),
        (0.15, bool(lineas) and "lineas" not in dudosos),                  # el detalle suma la base
        (0.05, bool(receptor)),                                            # a nombre de la empresa
        (0.05, bool(aprendido)),                                           # coincide con lo aprendido de este proveedor
    ]
    conf = sum(p_ for p_, ok_ in chequeos if ok_) + (0.05 if not aprendido and cierra else 0)
    if tot and tot.get("n_candidatos", 0) > 6 and not cierra:
        conf -= 0.1
        obs.append("Varias combinaciones de importes posibles: verificar base y total.")
    if not cierra and tot:
        dudosos.append("importes_sin_cierre")
    ret_g, irpf = tot.get("ret_g"), tot.get("irpf")
    return {
        "tipo_documento": tipo,
        "emisor": {"nombre": nombre_em, "nif": emisor, "iban": iban},
        "receptor": {"nombre": _nombre_receptor(texto) if receptor else None, "nif": receptor},
        "numero": numero, "fecha": fecha.isoformat() if fecha else None,
        "fecha_vencimiento": venc.isoformat() if venc else None,
        "obra": {"texto_literal": obra_txt, "codigo": None},
        "concepto_general": _concepto(texto),
        "forma_pago": forma_pago, "pedido_contrato": pedido, "albaranes": albs,
        "lineas": lineas,
        "base_imponible": str(base) if base is not None else None,
        "impuestos": [{"tipo_pct": str(tot["tipo"]), "base": str(base), "cuota": str(tot["cuota"])}] if tot else [],
        "inversion_sujeto_pasivo": bool(tot.get("isp")),
        "exencion_motivo": "Exenta según el documento" if tot.get("exento") else None,
        "total_factura": str(tot["total"]) if tot else None,
        "retencion_garantia": {"pct": str(ret_g["pct"]), "base": str(base), "importe": str(ret_g["importe"])} if ret_g else {},
        "irpf": {"pct": str(irpf["pct"]), "importe": str(irpf["importe"])} if irpf else {},
        "total_a_pagar": str(tot["liquido"]) if tot else None,
        "confianza": round(max(0.05, min(conf, 0.99)), 2),
        "_cierra": cierra,
        "campos_dudosos": dudosos,
        "observaciones": " ".join(obs) or None,
    }


def _concepto(texto: str) -> str:
    for ln in texto.splitlines():
        s = re.sub(r"\s{2,}", " ", ln).strip()
        if re.search(r"(trabajos|suministro|certificaci|instalaci|montaje|servicio|alquiler|limpieza|horas|material)", s, re.I) \
                and 10 < len(s) < 160 and not re.search(r"protecci[oó]n de datos|responsable|tratamiento", s, re.I):
            return re.sub(r"\s*-?\d[\d.,]*\s*€?\s*$", "", s)[:150]
    return "Importe según factura (sin desglose)"


# ============================================================================ 3. IA LOCAL (Ollama)
def ollama_disponible(url: str) -> list[str]:
    """Modelos instalados en Ollama, o [] si no está arrancado."""
    from .ollama_cliente import modelos
    return modelos(url)


ESQUEMA_HUECOS = {
    "type": "object",
    "properties": {
        "tipo_documento": {"type": "string", "enum": ["factura", "abono", "anticipo", "proforma", "certificacion", "albaran",
                                                       "parte_horas", "presupuesto", "otro"]},
        "emisor_nombre": {"type": ["string", "null"]}, "emisor_nif": {"type": ["string", "null"]},
        "numero": {"type": ["string", "null"]}, "fecha": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "fecha_vencimiento": {"type": ["string", "null"]},
        "base_imponible": {"type": ["number", "null"]}, "tipo_iva": {"type": ["number", "null"]},
        "cuota_iva": {"type": ["number", "null"]}, "total_factura": {"type": ["number", "null"]},
        "retencion_garantia": {"type": ["number", "null"]}, "irpf": {"type": ["number", "null"]},
        "total_a_pagar": {"type": ["number", "null"]}, "obra": {"type": ["string", "null"]},
        "inversion_sujeto_pasivo": {"type": "boolean"},
    },
    "required": ["tipo_documento", "emisor_nombre", "emisor_nif", "numero", "fecha", "base_imponible", "total_factura"],
}

PROMPT_HUECOS = """Eres un contable. Lee la factura y devuelve SOLO un JSON con estos campos (null si no aparece):
tipo_documento, emisor_nombre, emisor_nif (quien emite, NO LLORCA GROUP HISPANIA B54727722), numero, fecha (YYYY-MM-DD),
fecha_vencimiento, base_imponible, tipo_iva, cuota_iva, total_factura (base + IVA), retencion_garantia, irpf,
total_a_pagar, obra (texto de la referencia de obra), inversion_sujeto_pasivo.
Importes como número con punto decimal (1234.56). Copia los importes impresos: no calcules."""


def _modelo_vision(modelo: str) -> bool:
    m = (modelo or "").lower()
    return any(x in m for x in ("vision", "-vl", "vl:", "llava", "moondream", "minicpm-v", "gemma3"))


def _imagenes(data: bytes, paginas: list[int], max_lado: int = 1500) -> list[str]:
    """Renderiza páginas para los modelos Vision.

    Para facturas normales se mandan todas las páginas (hasta 8). En documentos largos
    se conservan las primeras y últimas para evitar explosiones de RAM/contexto.
    """
    import base64
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    n = len(pdf)
    wanted = []
    for p in paginas:
        if -n <= p < n:
            wanted.append(p % n)
    wanted = sorted(set(wanted))
    out = []
    for i in wanted:
        img = pdf[i].render(scale=2.4).to_pil().convert("RGB")
        r = max_lado / max(img.size)
        if r < 1:
            img = img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=84, optimize=True)
        out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
    return out


def _paginas_vision(data: bytes, max_paginas: int = 8) -> list[int]:
    import pypdfium2 as pdfium
    n = len(pdfium.PdfDocument(data))
    if n <= max_paginas:
        return list(range(n))
    # Para PDFs largos: cabecera, zona central y pie. Evita mandar 50 páginas a un 4B.
    mitad = max(1, max_paginas // 2)
    return list(range(mitad)) + list(range(max(0, n - mitad), n))

def _texto_compacto(texto: str, cab: int = 3500, pie: int = 3000) -> str:
    """Cabecera (datos del emisor, nº, fecha) + pie (totales): lo que hace falta para los huecos."""
    t = re.sub(r"[ \t]{3,}", "   ", texto or "")
    return t if len(t) <= cab + pie else t[:cab] + "\n[...]\n" + t[-pie:]


def motor_ollama(texto: str, data: bytes, url: str, modelo: str, opciones: dict | None = None,
                 metodo: str = "texto", timeout: int = 420, campos: list[str] | None = None,
                 memoria: str = "", verificacion: bool = False) -> dict:
    """Lectura local estructurada con Qwen3-VL.

    La memoria contiene ejemplos aprobados, pero el documento actual siempre manda:
    los ejemplos sirven para reconocer formatos, nunca para inventar importes.
    """
    from .ollama_cliente import chat, parse_json
    campos = [c for c in (campos or list(ESQUEMA_HUECOS["properties"])) if c in ESQUEMA_HUECOS["properties"]]
    if "tipo_documento" not in campos:
        campos = ["tipo_documento"] + campos
    esquema = {"type": "object", "properties": {c: ESQUEMA_HUECOS["properties"][c] for c in campos}, "required": campos}
    instr = (
        "Eres el lector local de facturas de LLORCA GROUP. Devuelve SOLO JSON. "
        "Lee el documento actual, no inventes ni calcules importes. Copia los importes impresos. "
        "El emisor es quien factura, NUNCA LLORCA GROUP HISPANIA (B54727722). "
        "Fechas YYYY-MM-DD. Importes con punto decimal. "
        + ("ESTA ES UNA SEGUNDA VERIFICACIÓN: busca específicamente errores de lectura de base, IVA, retención y total. " if verificacion else "")
        + "Campos: " + ", ".join(campos) + "."
    )
    if memoria:
        instr += ("\n\nEJEMPLOS VALIDADOS DE FACTURAS ANTERIORES. ÚSALOS SOLO PARA RECONOCER EL FORMATO "
                  "Y PATRONES DEL PROVEEDOR. NO COPIES NINGÚN IMPORTE NI DATO QUE NO APAREZCA EN EL DOCUMENTO ACTUAL:\n"
                  + memoria[:7000])
    opciones = {**(opciones or {}), "num_ctx": min(int((opciones or {}).get("num_ctx", 8192)), 8192), "num_predict": 700}
    msg = {"role": "user", "content": instr + "\n\n--- TEXTO EXTRAÍDO ---\n" + _texto_compacto(texto, 4500, 3500)}
    # Vision: si es escaneado, todas las páginas hasta 8; si hay texto, primeras/últimas como control visual.
    if _modelo_vision(modelo) and data:
        try:
            pags = _paginas_vision(data, 8 if metodo != "texto" else 6)
            msg["images"] = _imagenes(data, pags)
            msg["content"] += "\n\nHe adjuntado imágenes de las páginas relevantes. La imagen manda cuando el texto extraído esté desordenado."
        except Exception:
            pass
    m = chat(url, modelo, [msg], formato=esquema, opciones=opciones, timeout=timeout)
    return parse_json(m.get("content") or "")

def _cierra_totales(base, cuota, total, tipo=None) -> bool:
    try:
        b, c, t = Decimal(str(base)), Decimal(str(cuota or 0)), Decimal(str(total))
    except Exception:
        return False
    if abs(b + c - t) > Decimal("0.02"):
        return False
    return tipo is None or abs(q2(b * Decimal(str(tipo)) / 100) - c) <= Decimal("0.02")


def fusionar(reglas: dict, ia: dict) -> tuple[dict, list[str]]:
    """
    Reglas + IA local. Criterio: lo que CIERRA matemáticamente manda.
    - Campo vacío en reglas -> se toma de la IA (marcado como dudoso para revisión).
    - Importes: si los de reglas cierran, se quedan; si no cierran y los de la IA sí, se toman los de la IA.
    - Discrepancias en datos que mueven dinero -> se anotan para revisión.
    """
    d = dict(reglas)
    dudosos = list(d.get("campos_dudosos") or [])
    notas = []
    em = dict(d.get("emisor") or {})
    for k_ia, k in (("emisor_nombre", "nombre"), ("emisor_nif", "nif")):
        v = ia.get(k_ia)
        if v and (("LLORCA" in str(v).upper()) or normalize_nif(v) in EMPRESA_NIFS):
            notas.append(f"La IA propuso como emisor a la propia empresa ({v}): descartado")
            continue
        if v and not em.get(k):
            em[k] = normalize_nif(v) if k == "nif" and validate_nif(v)[0] else (v if k == "nombre" else em.get(k))
            if em.get(k):
                dudosos.append(f"emisor.{k}")
    d["emisor"] = em
    for k in ("numero", "fecha", "fecha_vencimiento"):
        if ia.get(k) and not d.get(k):
            d[k] = str(ia[k])
            dudosos.append(k)
        elif ia.get(k) and d.get(k) and str(ia[k]).replace(" ", "") != str(d[k]).replace(" ", ""):
            notas.append(f"{k}: reglas {d[k]} / IA {ia[k]}")
    if ia.get("obra") and not (d.get("obra") or {}).get("texto_literal"):
        d["obra"] = {"texto_literal": ia["obra"], "codigo": None}
    reglas_ok = d.get("base_imponible") is not None and _cierra_totales(
        d["base_imponible"], (d.get("impuestos") or [{}])[0].get("cuota", 0) if d.get("impuestos") else 0, d.get("total_factura") or 0)
    ia_ok = ia.get("base_imponible") is not None and ia.get("total_factura") is not None and \
        _cierra_totales(ia["base_imponible"], ia.get("cuota_iva") or 0, ia["total_factura"], ia.get("tipo_iva"))
    signo_distinto = False
    if reglas_ok and ia_ok and ia.get("tipo_documento") == "abono":
        try:
            signo_distinto = Decimal(str(ia["total_factura"])) < 0 <= Decimal(str(d.get("total_factura") or 0))
        except Exception:
            signo_distinto = False
        if signo_distinto:
            notas.append(f"las reglas leyeron {d.get('total_factura')} en positivo, pero la IA ve un abono de "
                         f"{ia['total_factura']}: se toman los importes de la IA")
    if (not reglas_ok or signo_distinto) and ia_ok:
        b = q2(Decimal(str(ia["base_imponible"])))
        d["base_imponible"] = str(b)
        d["impuestos"] = [{"tipo_pct": str(ia.get("tipo_iva") or 0), "base": str(b), "cuota": str(q2(Decimal(str(ia.get("cuota_iva") or 0))))}]
        d["total_factura"] = str(q2(Decimal(str(ia["total_factura"]))))
        tp = ia.get("total_a_pagar") or ia["total_factura"]
        d["total_a_pagar"] = str(q2(Decimal(str(tp))))
        if ia.get("retencion_garantia"):
            d["retencion_garantia"] = {"importe": str(q2(Decimal(str(ia["retencion_garantia"])))), "base": str(b), "pct": None}
        if ia.get("irpf"):
            d["irpf"] = {"importe": str(q2(Decimal(str(ia["irpf"])))), "pct": None}
        d["inversion_sujeto_pasivo"] = bool(ia.get("inversion_sujeto_pasivo")) or d.get("inversion_sujeto_pasivo")
        d["lineas"] = [{"descripcion": d.get("concepto_general") or "Importe según factura", "importe": str(b),
                        "tipo_linea": "normal", "partida_codigo": None, "partida_confianza": 0}]
        if b < 0:
            d["tipo_documento"] = "abono"
        dudosos = [x for x in dudosos if x not in ("base_imponible", "total_factura")] + ["importes_leidos_por_IA"]
    elif reglas_ok and ia_ok and q2(Decimal(str(ia["base_imponible"]))) != q2(Decimal(str(d["base_imponible"]))):
        notas.append(f"base imponible: reglas {d['base_imponible']} / IA {ia['base_imponible']} (se mantiene la que cierra)")
    if ia.get("tipo_documento") and d.get("tipo_documento") == "factura" and ia["tipo_documento"] in ("abono", "albaran", "parte_horas", "proforma"):
        notas.append(f"La IA lo considera «{ia['tipo_documento']}»")
    d["campos_dudosos"] = sorted(set(dudosos))
    if notas:
        d["observaciones"] = ((d.get("observaciones") or "") + " Contraste reglas/IA: " + "; ".join(notas)).strip()
    campos_clave = [d.get("base_imponible"), d.get("total_factura"), em.get("nif"), d.get("numero"), d.get("fecha")]
    d["confianza"] = round(min(0.95, max(float(d.get("confianza") or 0), 0.5 + 0.09 * sum(1 for x in campos_clave if x))), 2)
    return d, notas


def huecos(reglas: dict) -> list[str]:
    """Campos clave que las reglas no han podido fijar con seguridad."""
    h = []
    if not (reglas.get("emisor") or {}).get("nif"):
        h += ["emisor_nombre", "emisor_nif"]
    elif not (reglas.get("emisor") or {}).get("nombre"):
        h.append("emisor_nombre")
    for k in ("numero", "fecha"):
        if not reglas.get(k):
            h.append(k)
    if reglas.get("base_imponible") is None or not reglas.get("_cierra"):
        h += ["base_imponible", "tipo_iva", "cuota_iva", "total_factura", "retencion_garantia", "irpf", "total_a_pagar",
              "inversion_sujeto_pasivo"]
    return h


def necesita_ia(reglas: dict, metodo: str) -> bool:
    """IA local solo si faltan datos clave, los importes no cierran por ninguna ecuación o el PDF es escaneado.
    (Antes también se llamaba por «confianza < 0,85»: en CPU eso suponía ~90 s por factura sin necesidad.)"""
    return metodo != "texto" or bool(huecos(reglas))


# ============================================================================ orquestación
def _dec(v):
    try:
        return q2(Decimal(str(v))) if v not in (None, "") else None
    except Exception:
        return None


def extraer_local(data: bytes, obras: list[dict], partidas: list[dict], proveedores: dict | None = None,
                  motor: str = "auto", ollama_url: str = "http://localhost:11434", ollama_modelo: str = "qwen2.5:7b",
                  usar_ocr: bool = True, filename: str = "", ollama_opciones: dict | None = None,
                  ia_siempre: bool = False, timeout_ia: int = 420, plantillas: dict | None = None,
                  memoria: str = "") -> dict:
    """
    1) Texto (capa del PDF u OCR en CPU)  2) Reglas matemáticas (siempre, < 1 s)
    3) IA local SOLO si hace falta (faltan datos clave, no cierran los importes, escaneado) o si se pide siempre.
    """
    t0 = time.time()
    texto, metodo = obtener_texto(data, usar_ocr)
    reglas = motor_reglas(texto, partidas, proveedores, filename, plantillas)
    if float(reglas.get("confianza") or 0) < 0.85 and metodo == "texto":
        # SEGUNDA PASADA: el texto del PDF puede venir desordenado (columnas, letras giradas, fuentes raras).
        # Se prueban otras extracciones del mismo documento y se queda la de mayor confianza verificable.
        alternativas = []
        try:
            t_plano, _ = read_text(data)
            alternativas.append((t_plano, "texto"))
        except Exception:
            pass
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(data)
            alternativas.append(("\n".join(pdf[i].get_textpage().get_text_range() for i in range(min(len(pdf), 10))), "texto"))
        except Exception:
            pass
        if usar_ocr and float(reglas.get("confianza") or 0) < 0.7:
            try:
                import pypdfium2 as pdfium
                if len(pdfium.PdfDocument(data)) <= 4:
                    alternativas.append((ocr_pdf(data), "ocr"))
            except Exception:
                pass
        for t_alt, m_alt in alternativas:
            if not t_alt or len(t_alt) < 50:
                continue
            r_alt = motor_reglas(t_alt, partidas, proveedores, filename, plantillas)
            if float(r_alt.get("confianza") or 0) > float(reglas.get("confianza") or 0) + 0.04:
                reglas, texto = r_alt, t_alt
                reglas["observaciones"] = ((reglas.get("observaciones") or "") +
                                           f" Leído en segunda pasada ({'OCR' if m_alt == 'ocr' else 'texto alternativo'}).").strip()
    nombre = f"local · reglas · {'OCR' if metodo == 'ocr' else 'texto PDF'}"
    datos = reglas
    reglas.pop("_ia_no", None)
    if motor != "reglas" and (ia_siempre or necesita_ia(reglas, metodo)):
        from .ollama_cliente import elegir_modelo
        instalados = ollama_disponible(ollama_url)
        ollama_modelo = elegir_modelo(ollama_url, ollama_modelo) or ollama_modelo
        if ollama_modelo in instalados:
            try:
                ia = motor_ollama(texto, data, ollama_url, ollama_modelo, ollama_opciones, metodo, timeout_ia,
                                  None if ia_siempre else huecos(reglas) or None, memoria=memoria)
                datos, notas_ia = fusionar(reglas, ia)
                # Segunda lectura solo cuando hay un motivo real: PDF escaneado, descuadre o conflicto.
                # Así se gana robustez sin convertir cada factura en 2 inferencias lentas.
                necesita_verif = metodo != "texto" or not reglas.get("_cierra") or bool(notas_ia)
                if necesita_verif:
                    try:
                        ia2 = motor_ollama(texto, data, ollama_url, ollama_modelo, ollama_opciones, metodo, timeout_ia,
                                           ["base_imponible", "tipo_iva", "cuota_iva", "total_factura",
                                            "retencion_garantia", "irpf", "total_a_pagar", "numero", "fecha"],
                                           memoria=memoria, verificacion=True)
                        # Si dos lecturas independientes coinciden en los importes clave, se marca como contraste fuerte.
                        claves = ("base_imponible", "cuota_iva", "total_factura", "total_a_pagar")
                        iguales = all(ia.get(k) is not None and ia2.get(k) is not None and
                                      q2(Decimal(str(ia[k]))) == q2(Decimal(str(ia2[k]))) for k in claves)
                        if iguales:
                            datos["confianza"] = min(0.99, max(float(datos.get("confianza") or 0), 0.94))
                            datos["observaciones"] = ((datos.get("observaciones") or "") +
                                                       " Segunda lectura Vision coincidente en importes clave.").strip()
                        else:
                            datos["campos_dudosos"] = sorted(set((datos.get("campos_dudosos") or []) + ["contraste_ia_2"]))
                            datos["observaciones"] = ((datos.get("observaciones") or "") +
                                                       " Segunda lectura Vision no coincide completamente: revisión humana recomendada.").strip()
                    except Exception as e:
                        datos["observaciones"] = ((datos.get("observaciones") or "") +
                                                   f" Verificación secundaria no disponible: {str(e)[:120]}").strip()
                nombre = f"local · reglas + {ollama_modelo} + memoria · {'OCR' if metodo == 'ocr' else 'texto PDF'}"
            except Exception as e:  # noqa: BLE001
                reglas["_aviso_ia"] = f"IA local no disponible ({str(e)[:160]}); leído solo con reglas."
        elif instalados:
            reglas["_aviso_ia"] = f"El modelo «{ollama_modelo}» no está instalado en Ollama (hay: {', '.join(instalados)})."
    if metodo == "ocr":
        datos["campos_dudosos"] = list(datos.get("campos_dudosos") or []) + ["documento_escaneado_OCR"]
    datos.pop("_cierra", None)
    return {"data": datos, "modelo": nombre, "tokens_entrada": None, "tokens_salida": None,
            "segundos": round(time.time() - t0, 1), "metodo_texto": metodo, "texto": texto}
