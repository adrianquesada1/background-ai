from __future__ import annotations

"""
LLORCA - Extractor de facturas ULTRA MEJORADO v2

Pipeline híbrido diseñado para facturas españolas de construcción:

    PDF -> análisis por página -> texto nativo -> OCR selectivo
       -> reglas/regex -> tablas -> clasificación documental
       -> IA solo para ambigüedades -> validación matemática/fiscal
       -> JSON compatible con el extractor anterior

Principios:
- No se confía en una única llamada de IA para todo el documento.
- Los importes se conservan como cadenas decimales.
- Python calcula/comprueba; la IA transcribe/interpreta.
- OCR solo se ejecuta cuando la página realmente lo necesita.
- La evidencia de cada campo queda disponible en `_meta`.
- Compatible con Anthropic y Ollama/Qwen3-VL opcionalmente.
- Si la IA falla, el extractor sigue funcionando con reglas + OCR.

Dependencias recomendadas:
    pip install pymupdf pillow requests anthropic pytesseract

OCR:
    Instalar Tesseract OCR en Windows y añadir su carpeta al PATH.
    El extractor intenta usar el idioma spa y cae a eng si spa no está instalado.

La función pública mantiene la firma del extractor original:
    extraer_documento(pdf_bytes, api_key, model, obras, partidas, max_tokens=16000)

También se puede usar directamente:
    extraer_documento(..., backend="auto")
    extraer_documento(..., backend="ollama")
    extraer_documento(..., backend="anthropic")
    extraer_documento(..., backend="rules")
"""

import base64
import io
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Optional

try:
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover
    fitz = None
    _FITZ_IMPORT_ERROR = exc

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


# ---------------------------------------------------------------------------
# Excepciones / constantes
# ---------------------------------------------------------------------------

class ExtractionError(Exception):
    pass


NUM_RE = r"^-?\d+(?:\.\d+)?$"
NUM = {
    "type": ["string", "null"],
    "pattern": NUM_RE,
    "description": "Número decimal como cadena, sin separador de miles y con punto decimal.",
}
TXT = {"type": ["string", "null"]}

SUPPORTED_TYPES = [
    "factura", "abono", "anticipo", "proforma", "certificacion", "albaran",
    "parte_horas", "presupuesto", "otro"
]

# Esquema deliberadamente compatible con el esquema original.
SCHEMA = {
    "type": "object",
    "properties": {
        "tipo_documento": {"type": "string", "enum": SUPPORTED_TYPES},
        "emisor": {"type": "object", "properties": {
            "nombre": TXT, "nif": TXT, "direccion": TXT,
            "iban": {"type": ["string", "null"]},
        }, "required": ["nombre", "nif"]},
        "receptor": {"type": "object", "properties": {
            "nombre": TXT, "nif": TXT,
        }, "required": ["nombre", "nif"]},
        "numero": TXT,
        "fecha": TXT,
        "fecha_vencimiento": TXT,
        "periodo": TXT,
        "obra": {"type": "object", "properties": {
            "texto_literal": TXT, "codigo": TXT,
        }, "required": ["texto_literal", "codigo"]},
        "pedido_contrato": TXT,
        "presupuesto_referencia": TXT,
        "albaranes": {"type": "array", "items": {"type": "string"}},
        "factura_rectificada": TXT,
        "concepto_general": TXT,
        "lineas": {"type": "array", "items": {"type": "object", "properties": {
            "codigo": TXT,
            "descripcion": {"type": "string"},
            "cantidad": NUM,
            "unidad": TXT,
            "precio_unitario": NUM,
            "descuento_pct": NUM,
            "importe": {**NUM, "description": "Importe neto de línea tal como figura."},
            "tipo_linea": {"type": "string", "enum": [
                "normal", "extra", "descuento", "anticipo_deducido", "portes", "otro"
            ]},
            "es_extra": {"type": "boolean"},
            "albaran": TXT,
            "partida_codigo": TXT,
            "partida_confianza": {"type": "number", "minimum": 0, "maximum": 1},
        }, "required": ["descripcion", "importe", "tipo_linea", "partida_codigo", "partida_confianza"]}},
        "importe_bruto": NUM,
        "base_imponible": NUM,
        "impuestos": {"type": "array", "items": {"type": "object", "properties": {
            "tipo_pct": NUM, "base": NUM, "cuota": NUM,
            "recargo_pct": NUM, "recargo_cuota": NUM,
        }, "required": ["tipo_pct", "base", "cuota"]}},
        "inversion_sujeto_pasivo": {"type": "boolean"},
        "exencion_motivo": TXT,
        "total_factura": NUM,
        "irpf": {"type": "object", "properties": {"pct": NUM, "importe": NUM}},
        "retencion_garantia": {"type": "object", "properties": {
            "pct": NUM, "base": NUM, "importe": NUM,
        }},
        "total_a_pagar": NUM,
        "forma_pago": TXT,
        "confianza": {"type": "number", "minimum": 0, "maximum": 1},
        "campos_dudosos": {"type": "array", "items": {"type": "string"}},
        "observaciones": TXT,
    },
    "required": [
        "tipo_documento", "emisor", "receptor", "numero", "fecha", "obra", "lineas",
        "base_imponible", "impuestos", "inversion_sujeto_pasivo", "total_factura",
        "total_a_pagar", "confianza", "campos_dudosos"
    ],
}

SYSTEM = """Eres un técnico contable experto en facturación de empresas constructoras españolas.
Tu tarea es TRANSCRIBIR y desambiguar datos documentales al esquema indicado.

REGLAS CRÍTICAS:
1. No inventes. Si no aparece, usa null.
2. Copia los importes; no calcules para sustituir un importe impreso.
3. Convierte 1.234,56 -> 1234.56.
4. Conserva signos negativos de abonos, descuentos, anticipos y deducciones.
5. Incluye todas las líneas con importe que se puedan identificar.
6. Retenciones, IRPF e IVA van en sus campos, no como líneas.
7. Una certificación de obra NO debe tratarse como una factura normal si tiene
   ejecutado/certificaciones anteriores/entrega a cuenta.
8. Detecta inversión del sujeto pasivo solo si existe evidencia textual.
9. Diferencia emisor de receptor; no uses el nombre de LLORCA por defecto.
10. Para cada campo dudoso, añádelo a campos_dudosos.
11. Las listas de obras y partidas son auxiliares: nunca inventes una coincidencia.
"""


# ---------------------------------------------------------------------------
# Utilidades numéricas y texto
# ---------------------------------------------------------------------------

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def clean_space(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).replace("\u00a0", " ").replace("\u200b", "")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def norm_text(s: str) -> str:
    s = clean_space(s)
    return s.replace("º", "o").replace("ª", "a")


def norm_upper(s: str) -> str:
    return norm_text(s).upper()


def parse_decimal(raw: Any) -> Optional[Decimal]:
    if raw is None:
        return None
    s = clean_space(raw)
    if not s:
        return None
    s = s.replace("€", "").replace("%", "").replace(" ", "")
    # Formato europeo: 1.234,56 / -1.234,56
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    # OCR puede producir apóstrofes como separador de miles.
    s = s.replace("'", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def dec_str(value: Optional[Decimal], places: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    if places is not None:
        q = Decimal("1").scaleb(-places)
        value = value.quantize(q, rounding=ROUND_HALF_UP)
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def money(value: Optional[Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def nearly(a: Optional[Decimal], b: Optional[Decimal], cents: str = "0.02") -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= Decimal(cents)


def first_nonempty(*values: Any) -> Optional[str]:
    for v in values:
        if clean_space(v):
            return clean_space(v)
    return None


def unique(seq: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for x in seq:
        x = clean_space(x)
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def normalize_number(raw: Any) -> Optional[str]:
    d = parse_decimal(raw)
    return dec_str(d)


# ---------------------------------------------------------------------------
# Modelo interno de páginas
# ---------------------------------------------------------------------------

@dataclass
class PageData:
    number: int
    text: str = ""
    text_chars: int = 0
    image_count: int = 0
    width: float = 0
    height: float = 0
    needs_ocr: bool = False
    ocr_text: str = ""
    ocr_used: bool = False
    render_b64: Optional[str] = None

    @property
    def combined(self) -> str:
        if self.ocr_text and len(self.ocr_text) > len(self.text) * 0.4:
            return (self.text + "\n" + self.ocr_text).strip()
        return (self.text + "\n" + self.ocr_text).strip()


@dataclass
class DocumentAnalysis:
    pages: list[PageData] = field(default_factory=list)
    text: str = ""
    classification: str = "otro"
    score: float = 0.0
    source: str = "pdf_text"
    warnings: list[str] = field(default_factory=list)
    processing_seconds: float = 0.0

    @property
    def page_count(self) -> int:
        return len(self.pages)


# ---------------------------------------------------------------------------
# PDF + OCR
# ---------------------------------------------------------------------------

class PDFAnalyzer:
    def __init__(self, ocr_enabled: bool = True, ocr_dpi: int = 180,
                 ocr_min_chars: int = 80, ocr_force_if_image_ratio: float = 0.60):
        self.ocr_enabled = ocr_enabled
        self.ocr_dpi = ocr_dpi
        self.ocr_min_chars = ocr_min_chars
        self.ocr_force_if_image_ratio = ocr_force_if_image_ratio

    def analyze(self, pdf_bytes: bytes) -> DocumentAnalysis:
        if fitz is None:
            raise ExtractionError(f"PyMuPDF no disponible: {_FITZ_IMPORT_ERROR}")
        t0 = time.time()
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as exc:
            raise ExtractionError(f"No se pudo abrir el PDF: {exc}") from exc

        pages: list[PageData] = []
        for i, page in enumerate(doc):
            text = page.get_text("text", sort=True) or ""
            rect = page.rect
            images = page.get_images(full=True)
            chars = len(re.sub(r"\s+", "", text))
            # Si una página tiene poquísimo texto y contiene una imagen grande,
            # es muy probable que sea un escaneado.
            page_area = max(float(rect.width * rect.height), 1.0)
            image_area = 0.0
            for img in images:
                try:
                    xref = img[0]
                    pix = fitz.Pixmap(doc, xref)
                    image_area += float(pix.width * pix.height)
                    pix = None
                except Exception:
                    pass
            # La métrica de área de píxeles no es geométricamente exacta, así
            # que se usa como señal junto con el número de caracteres.
            image_signal = image_area / max(page_area * 2.0, 1.0)
            needs = chars < self.ocr_min_chars or (len(images) > 0 and image_signal > self.ocr_force_if_image_ratio and chars < 500)
            pages.append(PageData(
                number=i + 1,
                text=text,
                text_chars=chars,
                image_count=len(images),
                width=float(rect.width),
                height=float(rect.height),
                needs_ocr=needs,
            ))

        analysis = DocumentAnalysis(pages=pages)
        self._ocr_needed_pages(doc, analysis)
        doc.close()
        analysis.text = "\n\n".join(
            f"--- PAGINA {p.number} ---\n{p.combined}" for p in analysis.pages if p.combined.strip()
        )
        if any(p.ocr_used for p in analysis.pages):
            analysis.source = "pdf_text+ocr"
        elif not analysis.text.strip():
            analysis.source = "vision"
        analysis.processing_seconds = time.time() - t0
        return analysis

    def _ocr_needed_pages(self, doc: Any, analysis: DocumentAnalysis) -> None:
        if not self.ocr_enabled or pytesseract is None or Image is None:
            if any(p.needs_ocr for p in analysis.pages):
                analysis.warnings.append("Hay páginas con poco texto y OCR no está disponible.")
            return
        for pdata in analysis.pages:
            if not pdata.needs_ocr:
                continue
            try:
                page = doc[pdata.number - 1]
                matrix = fitz.Matrix(self.ocr_dpi / 72.0, self.ocr_dpi / 72.0)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                ocr = self._run_tesseract(image)
                if ocr.strip():
                    pdata.ocr_text = ocr.strip()
                    pdata.ocr_used = True
            except Exception as exc:
                analysis.warnings.append(f"OCR página {pdata.number}: {exc}")

    @staticmethod
    def _run_tesseract(image: Any) -> str:
        configs = [
            ("spa+eng", "--oem 3 --psm 6"),
            ("spa", "--oem 3 --psm 6"),
            ("eng", "--oem 3 --psm 6"),
        ]
        last_exc = None
        for lang, config in configs:
            try:
                return pytesseract.image_to_string(image, lang=lang, config=config)
            except Exception as exc:
                last_exc = exc
        if last_exc:
            raise last_exc
        return ""

    @staticmethod
    def page_images(pdf_bytes: bytes, pages: Iterable[int], dpi: int = 140) -> list[str]:
        """Devuelve imágenes PNG base64 de páginas concretas para modelos de visión."""
        if fitz is None:
            return []
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        result = []
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        for pno in pages:
            if pno < 1 or pno > len(doc):
                continue
            pix = doc[pno - 1].get_pixmap(matrix=matrix, alpha=False)
            result.append(base64.b64encode(pix.tobytes("png")).decode())
        doc.close()
        return result


# ---------------------------------------------------------------------------
# Clasificación documental
# ---------------------------------------------------------------------------

class DocumentClassifier:
    RULES = {
        "certificacion": [
            r"certificaci[oó]n", r"certificaci[oó]n n[ºo]", r"certificaciones anteriores",
            r"precio contratado", r"ejecutado", r"certificación del mes",
        ],
        "abono": [r"\babono\b", r"rectificativ", r"pronto pago", r"factura rectificada"],
        "anticipo": [r"\banticipo\b", r"entrega a cuenta", r"entrega\s+cuenta"],
        "proforma": [r"proforma", r"factura proforma"],
        "albaran": [r"\balbar[aá]n\b", r"nota de entrega"],
        "parte_horas": [r"parte de horas", r"horas trabajadas", r"\bhoras\b"],
        "presupuesto": [r"presupuesto", r"oferta económica", r"oferta"],
        "factura": [r"\bfactura\b", r"invoice", r"total factura", r"base imponible"],
    }

    def classify(self, text: str) -> tuple[str, float]:
        u = norm_upper(text)
        scores: dict[str, int] = {k: 0 for k in self.RULES}
        for kind, patterns in self.RULES.items():
            for pattern in patterns:
                if re.search(pattern, u, flags=re.I):
                    scores[kind] += 1
        # Prioridad de tipos específicos frente a factura genérica.
        priority = ["certificacion", "abono", "anticipo", "proforma", "albaran", "parte_horas", "presupuesto", "factura"]
        best = max(priority, key=lambda k: scores.get(k, 0))
        score = scores.get(best, 0)
        if score == 0:
            return "otro", 0.0
        # Evidencia adicional para tipos especiales.
        if best == "certificacion" and scores[best] >= 2:
            return best, min(0.98, 0.65 + scores[best] * 0.10)
        return best, min(0.95, 0.50 + score * 0.10)


# ---------------------------------------------------------------------------
# Regex extractor
# ---------------------------------------------------------------------------

class RuleExtractor:
    NIF_RE = re.compile(r"\b(?:[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]|\d{8}[A-Z])\b", re.I)
    DATE_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2}))\b")
    IBAN_RE = re.compile(r"\bES\s*\d{2}(?:\s*\d{4}){5}\s*\d{2}\b", re.I)

    def extract(self, text: str, obras: list[dict], partidas: list[dict], doc_type: str) -> dict:
        lines = [clean_space(x) for x in text.splitlines()]
        lines = [x for x in lines if x]
        u = "\n".join(lines)

        result = empty_result()
        result["tipo_documento"] = doc_type
        result["numero"] = self.invoice_number(u)
        result["fecha"] = self.date_for_labels(u, ["fecha de la factura", "fecha factura", "fecha emisión", "fecha de emisión", "fecha"])
        result["fecha_vencimiento"] = self.date_for_labels(u, ["vencimiento", "fecha vencimiento", "due date"])
        result["periodo"] = self.period(u)
        result["obra"] = self.obra(u, obras)
        result["pedido_contrato"] = self.label_value(u, ["pedido", "pedido nº", "pedido n", "orden de compra", "contrato"])
        result["presupuesto_referencia"] = self.label_value(u, ["presupuesto", "presupuesto nº", "presupuesto n"])
        result["albaranes"] = self.albarans(u)
        result["factura_rectificada"] = self.label_value(u, ["factura rectificada", "rectifica factura", "factura que rectifica"])
        result["concepto_general"] = self.concept(u, doc_type)
        result["emisor"], result["receptor"] = self.parties(u)
        result["forma_pago"] = self.label_value(u, ["forma de pago", "método de pago", "medio de pago", "condiciones de pago"])
        result["inversion_sujeto_pasivo"] = bool(re.search(
            r"inversi[oó]n\s+del\s+sujeto\s+pasiv[oa]|sujeto\s+pasivo[^\n]{0,80}destinatario|art\.?\s*84\s*\.?(?:uno)?\.?\s*2",
            u, re.I
        ))
        result["exencion_motivo"] = self.exemption(u)
        result["impuestos"] = self.taxes(u)
        result["retencion_garantia"] = self.retention(u)
        result["irpf"] = self.irpf(u)
        result["base_imponible"] = self.amount_label(u, [
            "base imponible", "base", "base imponible total", "suma base imponible"
        ])
        result["importe_bruto"] = self.amount_label(u, ["importe bruto", "subtotal", "suma", "importe total antes"])
        result["total_factura"] = self.amount_label(u, [
            "total factura", "total factura", "total con iva", "total iva incluido", "total"
        ])
        result["total_a_pagar"] = self.amount_label(u, [
            "total a pagar", "líquido a pagar", "importe a pagar", "neto a pagar", "total pagadero"
        ])
        if result["total_a_pagar"] is None:
            result["total_a_pagar"] = result["total_factura"]

        result["lineas"] = self.lines(lines, partidas)
        result["campos_dudosos"] = []
        return result

    def invoice_number(self, text: str) -> Optional[str]:
        # Prioridad absoluta a etiquetas explícitas. Permitimos saltos de línea porque
        # muchas facturas imprimen "FACTURA Nº FECHA FACTURA" y los valores debajo.
        patterns = [
            r"N(?:\.\s*[ºo°]|[ºo°])\s*(?:DE\s+)?FACTURA\s*[:#-]?\s*([A-Z0-9][A-Z0-9./_-]{1,30})",
            r"N[ÚU]MERO\s+DE\s+FACTURA\s*[:#-]?\s*([A-Z0-9][A-Z0-9./_-]{1,30})",
            r"FACTURA\s+N(?:\.\s*[ºo°]|[ºo°])\s*[:#-]?\s*([A-Z0-9][A-Z0-9./_-]{1,30})",
            r"(?:N(?:\.\s*[ºo°]|[ºo°])|N[ÚU]M\.?|NUM\.?)\s*[:#-]\s*([A-Z0-9][A-Z0-9./_-]{1,30})",
            r"\bFRA\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9./_-]{1,30})",
        ]
        bad = {"FECHA", "FACTURA", "CLIENTE", "NIF", "CIF", "TOTAL", "IMPORTE", "PAGAR", "FECHAFACTURA"}
        for p in patterns:
            m = re.search(p, text, re.I)
            if m:
                candidate = clean_space(m.group(1)).strip(".,;:")
                if norm_upper(candidate) not in bad and not re.fullmatch(r"(?:FECHA|FACTURA|CLIENTE|NIF|CIF)", candidate, re.I):
                    return candidate

        # Patrón muy frecuente: cabecera con "FACTURA Nº" y valor en la siguiente línea.
        m = re.search(r"FACTURA\s*N(?:\.\s*[ºo°]|[ºo°])[^\n]{0,80}\n\s*([A-Z0-9][A-Z0-9./_-]{1,30})\b", text, re.I)
        if m and norm_upper(m.group(1)) not in bad:
            return m.group(1).strip(".,;:")
        return None

    def date_for_labels(self, text: str, labels: list[str]) -> Optional[str]:
        label = "(?:" + "|".join(re.escape(x) for x in labels) + ")"
        # Buscar la fecha en una ventana pequeña después de la etiqueta.
        for m in re.finditer(label, text, re.I):
            window = text[m.end():m.end() + 220]
            dm = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2}))\b", window)
            if dm:
                return self.date_normalize(dm.group(1))
        return None

    @staticmethod
    def date_normalize(s: str) -> Optional[str]:
        try:
            a = re.split(r"[/-]", s)
            d, m, y = map(int, a)
            if y < 100:
                y += 2000
            return f"{y:04d}-{m:02d}-{d:02d}"
        except Exception:
            return None

    def period(self, text: str) -> Optional[str]:
        m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(20\d{2})\b", text, re.I)
        return f"{m.group(1).lower()} {m.group(2)}" if m else None

    def label_value(self, text: str, labels: list[str]) -> Optional[str]:
        label = r"\b(?:" + "|".join(re.escape(x) for x in labels) + r")\b"
        m = re.search(label + r"\s*[:#\-]?\s*([^\n]{2,100})", text, re.I)
        if not m:
            return None
        value = clean_space(m.group(1))
        value = re.split(r"\s{2,}|\b(?:fecha|importe|total|base|iva)\b", value, flags=re.I)[0]
        return value.strip(" .:;-\")") or None

    def obra(self, text: str, obras: list[dict]) -> dict:
        literal = self.label_value(text, ["obra", "proyecto", "centro de coste", "asunto", "referencia de obra"])
        codigo = None
        candidates = []
        for o in obras or []:
            code = clean_space(o.get("codigo"))
            name = clean_space(o.get("nombre"))
            aliases = clean_space(o.get("alias"))
            if not code:
                continue
            candidates.append((code, name, aliases))
            hay = norm_upper(text)
            if code and norm_upper(code) in hay:
                codigo = code
                break
            for term in [name, aliases]:
                if term and norm_upper(term) in hay:
                    codigo = code
                    break
            if codigo:
                break
        return {"texto_literal": literal, "codigo": codigo}

    def albarans(self, text: str) -> list[str]:
        found = []
        for m in re.finditer(r"(?:albar[aá]n|alb\.?|nota de entrega)\s*(?:n[ºo]\.?)?\s*[:#-]?\s*([A-Z0-9./_-]{2,30})", text, re.I):
            found.append(m.group(1))
        return unique(found)

    def concept(self, text: str, doc_type: str) -> Optional[str]:
        labels = ["concepto", "descripción", "detalle", "asunto", "trabajo realizado"]
        v = self.label_value(text, labels)
        if v:
            return v
        return {
            "certificacion": "Certificación de obra",
            "abono": "Abono/rectificación",
            "anticipo": "Anticipo/entrega a cuenta",
            "albaran": "Albarán",
            "proforma": "Factura proforma",
        }.get(doc_type)

    @staticmethod
    def _first_business_name(text: str) -> Optional[str]:
        bad = re.compile(r"FACTURA|INVOICE|CLIENTE|FECHA|DIRECCI[ÓO]N|POBLACI[ÓO]N|CIF|NIF|PARA|LLORCA|TOTAL|SALDO|ADEUDADO", re.I)
        for line in [clean_space(x) for x in text.splitlines()[:18]]:
            if not line or len(line) < 3 or line.startswith("--- PAGINA") or bad.search(line):
                continue
            if re.search(r"^N[ºO]", line, re.I):
                continue
            # Evitar líneas puramente numéricas o de dirección.
            if re.fullmatch(r"[0-9 .,/+\-=€]+", line) or ("=" in line and len(line) < 14) or re.search(r"^[-+]?€?\s*[\d.,]+\s*€?$", line):
                continue
            if re.search(r"^(AVDA|AV\.|C/|CALLE|POL[IÍ]GONO|PLAZA|PASEO)\b", line, re.I):
                continue
            return line
        return None

    def parties(self, text: str) -> tuple[dict, dict]:
        nif_values = unique([m.group(0).upper() for m in self.NIF_RE.finditer(text)])

        # Etiquetas explícitas primero.
        receptor_name = self.label_value(text, ["cliente", "receptor", "comprador", "facturar a", "facturado a"])
        if not receptor_name:
            pm = re.search(r"(?:^|\n)\s*PARA\s*\n\s*([^\n]{3,120})", text, re.I)
            if pm:
                receptor_name = clean_space(pm.group(1))
        receptor_nif = self._nif_after(text, ["cliente", "receptor", "comprador", "facturado a", "nif cliente", "nif receptor"])

        emisor_name = self.label_value(text, ["emisor", "proveedor", "vendedor", "facturado por"])
        emisor_nif = self._nif_after(text, ["emisor", "proveedor", "vendedor", "facturado por", "nif emisor"])

        # Heurística robusta para facturas con "CLIENTE:" en la derecha: el bloque
        # anterior suele ser el emisor. No devolvemos palabras de cabecera como nombre.
        if not emisor_name:
            emisor_name = self._first_business_name(text)
        if not emisor_name:
            m = re.search(r"(?:^|\n)\s*([^\n]{3,100})\s*\n(?:[^\n]*\n){0,3}\s*(?:CIF|NIF)\s*[:.]?\s*[A-Z0-9-]{8,12}", text, re.I)
            if m:
                candidate = clean_space(m.group(1))
                if not re.search(r"FACTURA|CLIENTE|DIRECCI[ÓO]N|POBLACI[ÓO]N|FECHA|TOTAL|SALDO|ADEUDADO", candidate, re.I):
                    emisor_name = candidate

        if not emisor_nif and emisor_name:
            pos = norm_upper(text).find(norm_upper(emisor_name))
            if pos >= 0:
                # Prefer an NIF/CIF explícitamente etiquetado en las primeras líneas del emisor.
                near = text[pos:pos + 900]
                # Si aparece CLIENTE antes del CIF, ese CIF pertenece al receptor.
                if re.search(r"\bCLIENTE\b", near, re.I):
                    near = re.split(r"\bCLIENTE\b", near, maxsplit=1, flags=re.I)[0]
                mm = re.search(r"(?:CIF|NIF|C\.I\.F\.|C\.I\.F\./N\.I\.F\.)\s*[:.]?\s*((?:[A-Z]\s*-?\s*\d{7}[0-9A-Z])|(?:[A-Z]\d{7}[0-9A-Z])|(?:\d{8}[A-Z]))", near, re.I)
                if mm:
                    emisor_nif = re.sub(r"\s+", "", mm.group(1)).upper()
        if not receptor_nif:
            # Si LLORCA aparece con su NIF, usarlo como receptor; es más fiable que el orden.
            m = re.search(r"LLORCA(?: GROUP)?(?: HISPANIA)?[\s\S]{0,250}?((?:[A-Z]\s*-?\s*\d{7}[0-9A-Z])|(?:[A-Z]\d{7}[0-9A-Z])|(?:\d{8}[A-Z]))", text, re.I)
            if m:
                receptor_nif = re.sub(r"\s+", "", m.group(1)).upper()
        if not receptor_nif and len(nif_values) > 1:
            receptor_nif = nif_values[1]

        if receptor_name and re.search(r"LLORCA", receptor_name, re.I):
            receptor_name = re.split(r"\s+N[ºo°]\s*(?:DE\s+FACTURA)?|\s+FECHA\b|\s+DIRECCI[ÓO]N\b", receptor_name, maxsplit=1, flags=re.I)[0].strip(" :,-")
        return (
            {"nombre": emisor_name, "nif": emisor_nif, "direccion": None, "iban": self.iban(text)},
            {"nombre": receptor_name or ("LLORCA GROUP HISPANIA S.L." if re.search(r"LLORCA GROUP HISPANIA", text, re.I) else None), "nif": receptor_nif},
        )

    def _nif_after(self, text: str, labels: list[str]) -> Optional[str]:
        label = r"\b(?:" + "|".join(re.escape(x) for x in labels) + r")\b"
        m = re.search(label + r"[^\n]{0,150}?((?:[A-Z]\d{7}[0-9A-Z])|(?:\d{8}[A-Z]))", text, re.I)
        return m.group(1).upper() if m else None

    def iban(self, text: str) -> Optional[str]:
        m = self.IBAN_RE.search(text)
        return re.sub(r"\s+", "", m.group(0)).upper() if m else None

    def exemption(self, text: str) -> Optional[str]:
        m = re.search(r"(?:exento|exenta|exenci[oó]n)[^\n]{0,160}", text, re.I)
        return clean_space(m.group(0)) if m else None

    def taxes(self, text: str) -> list[dict]:
        result = []
        patterns = [
            # IVA 21%: base 100 / cuota 21
            r"(?:iva|i\.v\.a\.)\s*(\d{1,2}(?:[.,]\d+)?)\s*%[^\n]{0,80}?([\d.]+,\d{2})[^\n]{0,50}?([\d.]+,\d{2})",
            # 21% ... cuota
            r"(\d{1,2}(?:[.,]\d+)?)\s*%\s*(?:iva)?[^\n]{0,80}?base[^\n]{0,50}?([\d.]+,\d{2})[^\n]{0,50}?cuota[^\n]{0,30}?([\d.]+,\d{2})",
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                pct = parse_decimal(m.group(1))
                base = parse_decimal(m.group(2))
                cuota = parse_decimal(m.group(3))
                if pct is not None and base is not None and cuota is not None:
                    item = {"tipo_pct": dec_str(pct), "base": dec_str(base), "cuota": dec_str(cuota), "recargo_pct": None, "recargo_cuota": None}
                    if not any(x["tipo_pct"] == item["tipo_pct"] and x["base"] == item["base"] for x in result):
                        result.append(item)
        # Detección ligera de “IVA 21%” si no hay base/cuota en la misma línea.
        if not result:
            for m in re.finditer(r"(?:iva|i\.v\.a\.)\s*(\d{1,2}(?:[.,]\d+)?)\s*%", text, re.I):
                pct = parse_decimal(m.group(1))
                result.append({"tipo_pct": dec_str(pct), "base": None, "cuota": None, "recargo_pct": None, "recargo_cuota": None})
        return result

    def retention(self, text: str) -> dict:
        patterns = [
            r"(?:retenci[oó]n|ret\.?)[^\n]{0,80}?(\d{1,2}(?:[.,]\d+)?)\s*%[^\n]{0,60}?(-?[\d.]+,\d{2})",
            r"(\d{1,2}(?:[.,]\d+)?)\s*%[^\n]{0,40}?retenci[oó]n[^\n]{0,40}?(-?[\d.]+,\d{2})",
        ]
        for p in patterns:
            m = re.search(p, text, re.I)
            if m:
                pct = parse_decimal(m.group(1)); amount = parse_decimal(m.group(2))
                base = None
                if pct and amount:
                    base = money(amount / (pct / Decimal(100)))
                return {"pct": dec_str(pct), "base": dec_str(base), "importe": dec_str(amount)}
        return {"pct": None, "base": None, "importe": None}

    def irpf(self, text: str) -> dict:
        m = re.search(r"(?:irpf|i\.r\.p\.f\.)[^\n]{0,70}?(\d{1,2}(?:[.,]\d+)?)\s*%[^\n]{0,60}?(-?[\d.]+,\d{2})", text, re.I)
        if not m:
            return {"pct": None, "importe": None}
        return {"pct": dec_str(parse_decimal(m.group(1))), "importe": dec_str(parse_decimal(m.group(2)))}

    def _table_total_value(self, text: str, field: str) -> Optional[str]:
        # Muchas facturas ponen una fila de cabeceras y debajo una fila solo numérica.
        # Usamos la posición de las columnas para evitar confundir "IVA 21%" con 21 euros.
        for m in re.finditer(r"(?im)^\s*(?:TOTAL IMPORTES\s+)?(?P<header>.*(?:BASE IMPONIBLE|TOTAL FACTURA).*)$", text):
            header = m.group("header")
            header_pos = {
                "base": re.search(r"BASE\s+IMPONIBLE", header, re.I),
                "total": re.search(r"TOTAL\s+FACTURA", header, re.I),
            }
            if field not in header_pos or not header_pos[field]:
                continue
            # Siguiente línea no vacía.
            tail = text[m.end():]
            nm = re.search(r"\n\s*([^\n]+)", tail)
            if not nm:
                continue
            row = nm.group(1)
            nums = list(re.finditer(r"-?\s*[\d.]+(?:,\d{1,2})?", row))
            if len(nums) < 2:
                continue
            # La tabla suele estar ordenada: total importes, base, % IVA, cuota, ret, total.
            if field == "base":
                # "TOTAL IMPORTES" suele añadir una primera columna de suma total;
                # en ese formato la base es la segunda. En cabeceras simples, la base es la primera.
                if re.search(r"TOTAL\s+IMPORTES", header, re.I) and len(nums) >= 2:
                    return normalize_number(nums[1].group(0))
                return normalize_number(nums[0].group(0))
            if field == "total":
                return normalize_number(nums[-1].group(0))

        # Variante habitual: BASE IMPONIBLE ... TOTAL FACTURA y una fila numérica.
        for m in re.finditer(r"(?im)^\s*(?P<header>.*BASE\s+IMPONIBLE.*TOTAL\s+FACTURA.*)$", text):
            tail = text[m.end():]
            nm = re.search(r"\n\s*([^\n]+)", tail)
            if nm:
                nums = list(re.finditer(r"-?\s*[\d.]+(?:,\d{1,2})?", nm.group(1)))
                if nums:
                    return normalize_number(nums[1].group(0) if field == "base" and len(nums) > 1 else nums[-1].group(0))
        return None

    def amount_label(self, text: str, labels: list[str]) -> Optional[str]:
        low_labels = {x.lower() for x in labels}
        if "base imponible" in low_labels:
            table_val = self._table_total_value(text, "base")
            if table_val is not None:
                return table_val
        if "total factura" in low_labels:
            table_val = self._table_total_value(text, "total")
            if table_val is not None:
                return table_val
        # No usar "total" como etiqueta genérica salvo como último recurso:
        # aparece constantemente en "total importes" antes del valor real.
        ordered = [x for x in labels if x.lower() != "total"] + (["total"] if "total" in [x.lower() for x in labels] else [])
        for lab in ordered:
            if lab.lower() == "total":
                continue
            pat = re.escape(lab) + r"[^\n]{0,120}?(-?\s*[\d.]+(?:,\d{1,2})?\s*(?:€|EUR)?)(?!\s*%)\b"
            matches = list(re.finditer(pat, text, re.I))
            if matches:
                vals = [normalize_number(m.group(1)) for m in matches]
                vals = [v for v in vals if v is not None]
                if vals:
                    return vals[-1]

        # "TOTAL FACTURA" puede aparecer como cabecera y el valor una línea después.
        for lab in [x for x in labels if x.lower() != "total"]:
            m = re.search(re.escape(lab) + r"[^\n]{0,160}\n\s*[^\n]{0,120}?(-?\s*[\d.]+(?:,\d{1,2})?)", text, re.I)
            if m:
                val = normalize_number(m.group(1))
                if val is not None:
                    return val

        # Último recurso: líneas que empiezan por TOTAL y tienen un único importe.
        for line in text.splitlines():
            if re.search(r"^\s*total(?:\s+factura)?\b", line, re.I):
                vals = re.findall(r"-?\s*[\d.]+(?:,\d{1,2})?", line)
                if vals:
                    return normalize_number(vals[-1])
        return None

    def lines(self, lines: list[str], partidas: list[dict]) -> list[dict]:
        out = []
        # Detecta filas con varios números al final. Es deliberadamente conservador.
        row_re = re.compile(
            r"^(?:(?P<code>[A-Z0-9][A-Z0-9._/-]{2,30})\s+)?"
            r"(?P<desc>.+?)\s+"
            r"(?P<qty>-?[\d.]+(?:,\d+)?)\s+"
            r"(?P<unit>[A-Za-zÁÉÍÓÚáéíóúñÑ²³%./-]{1,8})?\s*"
            r"(?P<price>-?[\d.]+(?:,\d+)?)\s+"
            r"(?P<amount>-?[\d.]+(?:,\d+)?)$"
        )
        for line in lines:
            if len(line) < 8:
                continue
            if re.search(r"base imponible|total factura|total a pagar|retenci[oó]n|iva\b|subtotal|importe bruto", line, re.I):
                continue
            m = row_re.match(line)
            if not m:
                continue
            desc = clean_space(m.group("desc"))
            if len(desc) < 3:
                continue
            amount = normalize_number(m.group("amount"))
            if amount is None:
                continue
            qty = normalize_number(m.group("qty"))
            price = normalize_number(m.group("price"))
            code = clean_space(m.group("code")) or None
            low = norm_upper(desc)
            typ = "normal"
            if "ANTICIPO" in low or "ENTREGA A CUENTA" in low:
                typ = "anticipo_deducido"
            elif "DESCUENTO" in low or "DTO." in low or "BONIFIC" in low:
                typ = "descuento"
            elif "PORTES" in low or "TRANSPORTE" in low:
                typ = "portes"
            elif "EXTRA" in low or "FUERA DE PRESUPUESTO" in low:
                typ = "extra"
            partida_code, conf = match_partida(desc, partidas)
            out.append({
                "codigo": code,
                "descripcion": desc,
                "cantidad": qty,
                "unidad": clean_space(m.group("unit")) or None,
                "precio_unitario": price,
                "descuento_pct": None,
                "importe": amount,
                "tipo_linea": typ,
                "es_extra": typ == "extra",
                "albaran": None,
                "partida_codigo": partida_code,
                "partida_confianza": conf,
            })
        return out


# ---------------------------------------------------------------------------
# IA local Ollama / Anthropic
# ---------------------------------------------------------------------------

class LocalAI:
    def __init__(self, model: str = "qwen3-vl:4b-instruct", host: str = "http://127.0.0.1:11434",
                 timeout: int = 180, num_ctx: int = 4096):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.num_ctx = num_ctx

    def available(self) -> bool:
        if requests is None:
            return False
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=3)
            return r.ok
        except Exception:
            return False

    def extract(self, text: str, obras: list[dict], partidas: list[dict],
                images_b64: Optional[list[str]] = None) -> dict:
        if requests is None:
            raise ExtractionError("requests no está instalado; Ollama no disponible.")
        prompt = self._prompt(text, obras, partidas)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        if images_b64:
            # Ollama acepta imágenes base64 en mensajes multimodales.
            messages[0]["images"] = images_b64[:4]
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_ctx": self.num_ctx,
                "num_gpu": 0,
            },
        }
        last = None
        for ctx in [self.num_ctx, 3072, 2048]:
            payload["options"]["num_ctx"] = ctx
            try:
                r = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
                if not r.ok:
                    last = f"HTTP {r.status_code}: {r.text[:500]}"
                    continue
                body = r.json()
                content = body.get("message", {}).get("content", "")
                data = parse_json_loose(content)
                if isinstance(data, dict):
                    return data
                last = "Ollama no devolvió JSON válido."
            except requests.Timeout as exc:
                last = f"timeout: {exc}"
            except Exception as exc:
                last = str(exc)
        raise ExtractionError(f"IA local no disponible tras reintentos: {last}")

    @staticmethod
    def _prompt(text: str, obras: list[dict], partidas: list[dict]) -> str:
        obras_txt = "\n".join(
            f"- {o.get('codigo')}: {o.get('nombre')} (alias: {o.get('alias') or '-'})" for o in obras or []
        ) or "- ninguna"
        partidas_txt = "\n".join(
            f"- {p.get('codigo')}: {p.get('descripcion')}" for p in partidas or []
        ) or "- 99: Sin asignar"
        return f"""{SYSTEM}

OBRAS:
{obras_txt}

PARTIDAS:
{partidas_txt}

DOCUMENTO:
{text[:50000]}

Devuelve SOLO JSON válido con estos campos:
{json.dumps(SCHEMA['properties'], ensure_ascii=False)}
"""


class AnthropicAI:
    def __init__(self, api_key: str, model: str, max_tokens: int = 16000, timeout: int = 600):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout

    def extract(self, pdf_bytes: bytes, obras: list[dict], partidas: list[dict], text: str) -> dict:
        try:
            import anthropic
        except Exception as exc:
            raise ExtractionError(f"anthropic no está instalado: {exc}") from exc
        if not self.api_key:
            raise ExtractionError("Falta la API key de Anthropic.")
        client = anthropic.Anthropic(api_key=self.api_key, max_retries=3, timeout=self.timeout)
        prompt = self._prompt(obras, partidas, text)
        content = [
            {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf_bytes).decode(),
            }},
            {"type": "text", "text": prompt},
        ]
        try:
            with client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0,
                system=SYSTEM,
                tools=[{"name": "registrar_documento", "description": "Registra los datos transcritos.", "input_schema": SCHEMA}],
                tool_choice={"type": "tool", "name": "registrar_documento"},
                messages=[{"role": "user", "content": content}],
            ) as stream:
                msg = stream.get_final_message()
        except Exception as exc:
            raise ExtractionError(f"Error de Anthropic: {exc}") from exc
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "registrar_documento":
                return block.input
        raise ExtractionError("Anthropic no devolvió datos estructurados.")

    @staticmethod
    def _prompt(obras: list[dict], partidas: list[dict], text: str) -> str:
        obras_txt = "\n".join(f"- {o.get('codigo')}: {o.get('nombre')}" for o in obras or []) or "- ninguna"
        partidas_txt = "\n".join(f"- {p.get('codigo')}: {p.get('descripcion')}" for p in partidas or []) or "- 99: Sin asignar"
        return f"{SYSTEM}\n\nOBRAS:\n{obras_txt}\n\nPARTIDAS:\n{partidas_txt}\n\nTexto auxiliar extraído localmente:\n{text[:60000]}"


# ---------------------------------------------------------------------------
# Validación y reconciliación
# ---------------------------------------------------------------------------

class Validator:
    def validate(self, data: dict, source_text: str, analysis: DocumentAnalysis) -> dict:
        data = normalize_result(data)
        checks: list[dict] = []
        warnings: list[str] = []

        # 1. Líneas
        line_sum = Decimal("0")
        for line in data.get("lineas", []):
            d = parse_decimal(line.get("importe"))
            if d is not None:
                line_sum += d
        base = parse_decimal(data.get("base_imponible"))
        total = parse_decimal(data.get("total_factura"))
        payable = parse_decimal(data.get("total_a_pagar"))

        if data.get("lineas") and base is not None:
            ok = nearly(money(line_sum), money(base), "0.03")
            checks.append({"nombre": "suma_lineas_vs_base", "ok": ok, "lineas": dec_str(money(line_sum)), "base": dec_str(money(base))})
            if not ok:
                warnings.append("La suma de las líneas no coincide exactamente con la base imponible; puede haber descuentos/portes/filas no estructuradas.")

        # 2. IVA por tipo
        iva_sum = Decimal("0")
        iva_known = False
        for tax in data.get("impuestos", []):
            cuota = parse_decimal(tax.get("cuota"))
            if cuota is not None:
                iva_sum += cuota
                iva_known = True
        if iva_known and base is not None and total is not None:
            expected = money(base + iva_sum)
            ok = nearly(expected, money(total), "0.03")
            checks.append({"nombre": "base_mas_iva_vs_total", "ok": ok, "calculado": dec_str(expected), "total": dec_str(money(total))})
            if not ok and not data.get("inversion_sujeto_pasivo"):
                warnings.append("Base + IVA no coincide con el total detectado.")

        # 3. Retención
        ret = data.get("retencion_garantia") or {}
        ret_amount = parse_decimal(ret.get("importe"))
        if ret_amount is not None and total is not None and payable is not None:
            expected = money(total - ret_amount)
            ok = nearly(expected, money(payable), "0.03")
            checks.append({"nombre": "retencion_vs_total_pagar", "ok": ok, "calculado": dec_str(expected), "total_a_pagar": dec_str(money(payable))})
            if not ok:
                warnings.append("La retención detectada no cuadra con el total a pagar; puede existir otra deducción.")

        # 4. IRPF
        irpf = data.get("irpf") or {}
        irpf_amount = parse_decimal(irpf.get("importe"))
        if irpf_amount is not None and total is not None and payable is not None and ret_amount is None:
            expected = money(total - irpf_amount)
            ok = nearly(expected, money(payable), "0.03")
            checks.append({"nombre": "irpf_vs_total_pagar", "ok": ok, "calculado": dec_str(expected), "total_a_pagar": dec_str(money(payable))})

        # 5. Inversión del sujeto pasivo
        if data.get("inversion_sujeto_pasivo"):
            if re.search(r"inversi[oó]n\s+del\s+sujeto\s+pasiv[oa]|art\.?\s*84", source_text, re.I):
                checks.append({"nombre": "inversion_sujeto_pasivo_evidencia", "ok": True})
            else:
                checks.append({"nombre": "inversion_sujeto_pasivo_evidencia", "ok": False})
                warnings.append("La IA marcó inversión del sujeto pasivo pero no se encontró evidencia textual clara.")
                data["inversion_sujeto_pasivo"] = False

        # 6. NIF y fecha como evidencia básica
        nifs = unique([m.group(0).upper() for m in RuleExtractor.NIF_RE.finditer(source_text)])
        if data.get("emisor", {}).get("nif") and nifs:
            if data["emisor"]["nif"].upper() not in nifs:
                warnings.append("El NIF del emisor no aparece literalmente en el texto extraído.")
        if data.get("receptor", {}).get("nif") and nifs:
            if data["receptor"]["nif"].upper() not in nifs:
                warnings.append("El NIF del receptor no aparece literalmente en el texto extraído.")

        data["_meta"] = {
            "version_extractor": "2.0-ultramejorado",
            "paginas": analysis.page_count,
            "fuente_texto": analysis.source,
            "paginas_ocr": [p.number for p in analysis.pages if p.ocr_used],
            "validaciones": checks,
            "advertencias": unique(warnings + analysis.warnings),
            "score_validacion": round(sum(1 for x in checks if x.get("ok")) / len(checks), 3) if checks else 0.0,
        }
        return data


# ---------------------------------------------------------------------------
# Match de partidas
# ---------------------------------------------------------------------------

def tokenize(s: str) -> set[str]:
    return {x for x in re.findall(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ0-9]{3,}", norm_upper(s)) if x not in {
        "PARA", "CON", "DEL", "LAS", "LOS", "UNA", "UN", "POR", "QUE", "DESDE", "HASTA"
    }}


def match_partida(description: str, partidas: list[dict]) -> tuple[str, float]:
    if not partidas:
        return "99", 0.0
    a = tokenize(description)
    if not a:
        return "99", 0.0
    best_code = "99"
    best = 0.0
    for p in partidas:
        b = tokenize(f"{p.get('descripcion', '')} {p.get('nombre', '')}")
        if not b:
            continue
        inter = len(a & b)
        score = inter / max(1, len(a | b))
        # Bonus por código mencionado literalmente.
        code = clean_space(p.get("codigo"))
        if code and norm_upper(code) in norm_upper(description):
            score += 0.75
        if score > best:
            best = score
            best_code = code or "99"
    return best_code if best >= 0.12 else "99", round(min(best, 1.0), 3)


# ---------------------------------------------------------------------------
# JSON / normalización
# ---------------------------------------------------------------------------

def parse_json_loose(text: str) -> Any:
    text = clean_space(text)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def empty_result() -> dict:
    return {
        "tipo_documento": "otro",
        "emisor": {"nombre": None, "nif": None, "direccion": None, "iban": None},
        "receptor": {"nombre": None, "nif": None},
        "numero": None,
        "fecha": None,
        "fecha_vencimiento": None,
        "periodo": None,
        "obra": {"texto_literal": None, "codigo": None},
        "pedido_contrato": None,
        "presupuesto_referencia": None,
        "albaranes": [],
        "factura_rectificada": None,
        "concepto_general": None,
        "lineas": [],
        "importe_bruto": None,
        "base_imponible": None,
        "impuestos": [],
        "inversion_sujeto_pasivo": False,
        "exencion_motivo": None,
        "total_factura": None,
        "irpf": {"pct": None, "importe": None},
        "retencion_garantia": {"pct": None, "base": None, "importe": None},
        "total_a_pagar": None,
        "forma_pago": None,
        "confianza": 0.0,
        "campos_dudosos": [],
        "observaciones": None,
    }


def normalize_result(data: Any) -> dict:
    result = empty_result()
    if not isinstance(data, dict):
        return result
    # Copiar únicamente claves conocidas.
    for key in result:
        if key in data:
            result[key] = data[key]
    # Normalización defensiva.
    if not isinstance(result.get("emisor"), dict):
        result["emisor"] = empty_result()["emisor"]
    if not isinstance(result.get("receptor"), dict):
        result["receptor"] = empty_result()["receptor"]
    if not isinstance(result.get("obra"), dict):
        result["obra"] = {"texto_literal": None, "codigo": None}
    if not isinstance(result.get("lineas"), list):
        result["lineas"] = []
    if not isinstance(result.get("impuestos"), list):
        result["impuestos"] = []
    if not isinstance(result.get("campos_dudosos"), list):
        result["campos_dudosos"] = []
    if not isinstance(result.get("albaranes"), list):
        result["albaranes"] = []
    result["tipo_documento"] = result.get("tipo_documento") if result.get("tipo_documento") in SUPPORTED_TYPES else "otro"

    numeric_fields = [
        "numero", "fecha", "fecha_vencimiento", "periodo", "pedido_contrato", "presupuesto_referencia",
        "factura_rectificada", "concepto_general", "forma_pago", "exencion_motivo", "observaciones"
    ]
    # Los anteriores son texto; los importes se normalizan aparte.
    for key in ["importe_bruto", "base_imponible", "total_factura", "total_a_pagar"]:
        if result.get(key) is not None:
            result[key] = normalize_number(result[key])
    for key in ["retencion_garantia", "irpf"]:
        obj = result.get(key)
        if not isinstance(obj, dict):
            obj = {"pct": None, "importe": None}
        for k in ["pct", "base", "importe"]:
            if k in obj and obj[k] is not None:
                obj[k] = normalize_number(obj[k])
        result[key] = obj
    for tax in result["impuestos"]:
        if isinstance(tax, dict):
            for k in ["tipo_pct", "base", "cuota", "recargo_pct", "recargo_cuota"]:
                if tax.get(k) is not None:
                    tax[k] = normalize_number(tax[k])
    for line in result["lineas"]:
        if not isinstance(line, dict):
            continue
        for k in ["cantidad", "precio_unitario", "descuento_pct", "importe"]:
            if line.get(k) is not None:
                line[k] = normalize_number(line[k])
        line.setdefault("tipo_linea", "normal")
        line.setdefault("es_extra", line.get("tipo_linea") == "extra")
        line.setdefault("partida_codigo", "99")
        try:
            line["partida_confianza"] = float(line.get("partida_confianza", 0))
        except Exception:
            line["partida_confianza"] = 0.0
    return result


def merge_rule_and_ai(rule_data: dict, ai_data: Optional[dict]) -> dict:
    """Fusiona IA y reglas sin dejar que una respuesta incompleta destruya evidencia local."""
    base = normalize_result(rule_data)
    if not isinstance(ai_data, dict):
        return base
    ai = normalize_result(ai_data)

    # Campos simples: IA solo gana si realmente devolvió valor.
    scalar = [
        "tipo_documento", "numero", "fecha", "fecha_vencimiento", "periodo", "pedido_contrato",
        "presupuesto_referencia", "factura_rectificada", "concepto_general", "importe_bruto",
        "base_imponible", "total_factura", "total_a_pagar", "forma_pago", "exencion_motivo", "observaciones"
    ]
    for k in scalar:
        if ai.get(k) not in (None, "", [], {}):
            base[k] = ai[k]

    for k in ["emisor", "receptor"]:
        if isinstance(ai.get(k), dict):
            for sub, value in ai[k].items():
                if value not in (None, ""):
                    base[k][sub] = value
    if isinstance(ai.get("obra"), dict):
        for sub, value in ai["obra"].items():
            if value not in (None, ""):
                base["obra"][sub] = value

    if ai.get("albaranes"):
        base["albaranes"] = unique(base.get("albaranes", []) + ai.get("albaranes", []))
    if ai.get("impuestos"):
        base["impuestos"] = ai["impuestos"]
    if isinstance(ai.get("retencion_garantia"), dict):
        for k, value in ai["retencion_garantia"].items():
            if value not in (None, ""):
                base["retencion_garantia"][k] = value
    if isinstance(ai.get("irpf"), dict):
        for k, value in ai["irpf"].items():
            if value not in (None, ""):
                base["irpf"][k] = value

    # Líneas: si IA ha producido líneas razonables, se prefieren para tablas complejas.
    if ai.get("lineas"):
        base["lineas"] = ai["lineas"]

    base["inversion_sujeto_pasivo"] = bool(ai.get("inversion_sujeto_pasivo") or base.get("inversion_sujeto_pasivo"))
    base["campos_dudosos"] = unique(base.get("campos_dudosos", []) + ai.get("campos_dudosos", []))
    return normalize_result(base)


# ---------------------------------------------------------------------------
# Confianza calculada
# ---------------------------------------------------------------------------

def compute_confidence(data: dict, analysis: DocumentAnalysis) -> float:
    scores = []
    if data.get("numero"): scores.append(1.0)
    if data.get("fecha"): scores.append(1.0)
    if data.get("emisor", {}).get("nombre"): scores.append(0.9)
    if data.get("emisor", {}).get("nif"): scores.append(1.0)
    if data.get("receptor", {}).get("nombre"): scores.append(0.9)
    if data.get("base_imponible"): scores.append(1.0)
    if data.get("total_factura"): scores.append(1.0)
    if data.get("total_a_pagar"): scores.append(1.0)
    if data.get("lineas"): scores.append(0.9)
    if data.get("obra", {}).get("codigo"): scores.append(0.95)

    validation = data.get("_meta", {}).get("score_validacion", 0.0)
    if scores:
        raw = sum(scores) / len(scores)
    else:
        raw = 0.25
    # OCR y visión son menos fiables que texto nativo, pero no penalizamos demasiado:
    source_penalty = 0.03 if analysis.source == "pdf_text+ocr" else (0.06 if analysis.source == "vision" else 0.0)
    final = max(0.0, min(1.0, raw * 0.70 + validation * 0.30 - source_penalty))
    return round(final, 3)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def _select_backend(backend: str, api_key: str, ollama_model: str) -> str:
    backend = (backend or "auto").lower()
    if backend in {"rules", "ollama", "anthropic"}:
        return backend
    if api_key:
        # Si se proporciona API key, se conserva compatibilidad con el extractor original.
        return "anthropic"
    return "ollama" if requests is not None else "rules"


def extraer_documento(
    pdf_bytes: bytes,
    api_key: str,
    model: str,
    obras: list[dict],
    partidas: list[dict],
    max_tokens: int = 16000,
    *,
    backend: str = "auto",
    ollama_model: Optional[str] = None,
    ollama_host: Optional[str] = None,
    ollama_timeout: int = 180,
    ollama_num_ctx: int = 4096,
    ocr_enabled: bool = True,
    ocr_dpi: int = 180,
    use_ai_only_when_needed: bool = True,
) -> dict:
    """Extrae una factura/documento usando el pipeline híbrido.

    Mantiene la salida principal del extractor anterior y añade `_meta` con:
    - páginas
    - páginas OCR
    - backend utilizado
    - validaciones
    - advertencias
    - tiempos
    """
    t0 = time.time()
    analyzer = PDFAnalyzer(ocr_enabled=ocr_enabled, ocr_dpi=ocr_dpi)
    analysis = analyzer.analyze(pdf_bytes)
    text = analysis.text

    classifier = DocumentClassifier()
    doc_type, class_score = classifier.classify(text)

    rules = RuleExtractor()
    rule_data = rules.extract(text, obras or [], partidas or [], doc_type)

    # Determinar si las reglas ya han resuelto el documento suficientemente.
    needs_ai, reasons = needs_ai_review(rule_data, analysis, class_score, use_ai_only_when_needed)
    selected = _select_backend(backend, api_key, ollama_model or "qwen3-vl:4b-instruct")
    ai_data = None
    ai_error = None
    ai_seconds = 0.0

    if selected != "rules" and needs_ai:
        ai_t0 = time.time()
        try:
            if selected == "ollama":
                ai = LocalAI(
                    model=ollama_model or os.getenv("OLLAMA_MODEL", "qwen3-vl:4b-instruct"),
                    host=ollama_host or os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"),
                    timeout=ollama_timeout,
                    num_ctx=ollama_num_ctx,
                )
                # Solo enviar imágenes si el texto es insuficiente. Esto evita el coste enorme
                # de mandar todas las páginas a visión.
                images = None
                if len(re.sub(r"\s+", "", text)) < 500:
                    low_pages = [p.number for p in analysis.pages if p.ocr_used or p.text_chars < 80][:4]
                    images = PDFAnalyzer.page_images(pdf_bytes, low_pages, dpi=140) if low_pages else None
                ai_data = ai.extract(text, obras or [], partidas or [], images_b64=images)
            else:
                ai = AnthropicAI(api_key=api_key, model=model, max_tokens=max_tokens)
                ai_data = ai.extract(pdf_bytes, obras or [], partidas or [], text)
        except Exception as exc:
            ai_error = str(exc)
        ai_seconds = round(time.time() - ai_t0, 2)

    merged = merge_rule_and_ai(rule_data, ai_data)
    # Correcciones deterministas para certificaciones: si la retención tiene una
    # base explícita/derivable y el total coincide con base-retención, ese patrón
    # es más fiable que interpretar "total" como base.
    if merged.get("tipo_documento") == "certificacion":
        ret = merged.get("retencion_garantia") or {}
        rb = parse_decimal(ret.get("base"))
        ra = parse_decimal(ret.get("importe"))
        tf = parse_decimal(merged.get("total_factura"))
        if rb is not None and ra is not None:
            if tf is not None and nearly(money(rb - ra), money(tf), "0.03"):
                merged["base_imponible"] = dec_str(rb)
                merged["total_factura"] = dec_str(rb)
                merged["total_a_pagar"] = dec_str(money(rb - ra))
            elif merged.get("base_imponible") is None:
                merged["base_imponible"] = dec_str(rb)
    validator = Validator()
    merged = validator.validate(merged, text, analysis)
    merged["confianza"] = compute_confidence(merged, analysis)

    # Recalcular campos dudosos automáticamente.
    doubts = list(merged.get("campos_dudosos", []))
    if not merged.get("numero"):
        doubts.append("numero")
    if not merged.get("fecha"):
        doubts.append("fecha")
    if not merged.get("emisor", {}).get("nombre"):
        doubts.append("emisor.nombre")
    if not merged.get("base_imponible"):
        doubts.append("base_imponible")
    if not merged.get("total_factura"):
        doubts.append("total_factura")
    if analysis.source != "pdf_text":
        doubts.append("lectura_con_OCR")
    if ai_error:
        doubts.append("IA_local/API: " + ai_error[:180])
    merged["campos_dudosos"] = unique(doubts)

    merged["_meta"].update({
        "backend_solicitado": backend,
        "backend_usado": selected,
        "ia_necesaria": needs_ai,
        "motivos_ia": reasons,
        "ia_ok": ai_data is not None,
        "ia_error": ai_error,
        "segundos_ia": ai_seconds,
        "segundos_total": round(time.time() - t0, 2),
        "clasificacion": {"tipo": doc_type, "score": class_score},
        "version_extractor": "2.0-ultramejorado",
    })
    return merged


def needs_ai_review(data: dict, analysis: DocumentAnalysis, class_score: float, enabled: bool) -> tuple[bool, list[str]]:
    if not enabled:
        return True, ["IA forzada por configuración"]
    reasons = []
    text_chars = len(re.sub(r"\s+", "", analysis.text))
    if text_chars < 300:
        reasons.append("poco texto disponible")
    if analysis.source != "pdf_text":
        reasons.append("OCR necesario")
    if class_score < 0.60:
        reasons.append("clasificación documental incierta")
    if not data.get("numero"):
        reasons.append("falta número")
    if not data.get("fecha"):
        reasons.append("falta fecha")
    if not data.get("emisor", {}).get("nombre"):
        reasons.append("falta emisor")
    if not data.get("base_imponible"):
        reasons.append("falta base")
    if not data.get("total_factura"):
        reasons.append("falta total")
    if not data.get("lineas") and text_chars > 500:
        reasons.append("no se detectaron líneas")

    # Documentos complejos de construcción merecen revisión de IA aunque los campos básicos estén.
    special = (
        data.get("tipo_documento") in {"certificacion", "abono", "anticipo"}
        or data.get("inversion_sujeto_pasivo")
        or data.get("retencion_garantia", {}).get("importe") is not None
        or len(analysis.pages) > 4
    )
    if special:
        reasons.append("documento complejo de obra/fiscal")
    return bool(reasons), unique(reasons)


# ---------------------------------------------------------------------------
# Compatibilidad: nombre antiguo + helper de prompt
# ---------------------------------------------------------------------------

def _user_prompt(obras: list[dict], partidas: list[dict]) -> str:
    lo = "\n".join(f"- {o.get('codigo')}: {o.get('nombre')} (alias: {o.get('alias') or '-'})" for o in obras or []) or "- (ninguna)"
    lp = "\n".join(f"- {p.get('codigo')}: {p.get('descripcion')}" for p in partidas or []) or "- 99: Sin asignar"
    return f"OBRAS DADAS DE ALTA:\n{lo}\n\nPARTIDAS:\n{lp}\n\nTranscribe el documento."


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extractor LLORCA ultra mejorado")
    parser.add_argument("pdf", help="PDF a extraer")
    parser.add_argument("--backend", default="rules", choices=["rules", "ollama", "anthropic", "auto"])
    parser.add_argument("--model", default="qwen3-vl:4b-instruct")
    parser.add_argument("--api-key", default=os.getenv("ANTHROPIC_API_KEY", ""))
    parser.add_argument("--output", default="-", help="JSON de salida o - para stdout")
    args = parser.parse_args()

    with open(args.pdf, "rb") as f:
        pdf = f.read()
    result = extraer_documento(
        pdf, args.api_key, args.model, [], [],
        backend=args.backend,
    )
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output == "-":
        print(out)
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
