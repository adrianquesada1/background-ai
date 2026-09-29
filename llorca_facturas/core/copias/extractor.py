"""
Extracción de facturas con Claude (API de Anthropic).

Diseño para minimizar errores:
1. Se envía el PDF ORIGINAL (Claude lee texto y también la imagen de cada página,
   así que funciona con PDFs escaneados sin instalar OCR).
2. Salida forzada a un esquema JSON mediante tool_choice (no texto libre).
3. Todos los importes se piden como CADENAS con punto decimal (nada de float).
4. Se exige copiar los importes TAL CUAL figuran; la IA NO debe calcular.
   Los cálculos y cuadres los hace después Python con aritmética exacta
   (core/validation.py), y los importes se anclan al texto del PDF.
"""
from __future__ import annotations

import base64
import json
import time

NUM = {"type": ["string", "null"], "pattern": r"^-?\d+(\.\d+)?$",
       "description": "Número con punto decimal y sin separador de miles, p.ej. '1234.56'. null si no aparece."}
TXT = {"type": ["string", "null"]}

SCHEMA = {
    "type": "object",
    "properties": {
        "tipo_documento": {
            "type": "string",
            "enum": ["factura", "abono", "anticipo", "proforma", "certificacion", "albaran",
                     "parte_horas", "presupuesto", "otro"],
            "description": "abono = factura rectificativa / negativa. anticipo = factura de entrega a cuenta. "
                           "certificacion/parte_horas/albaran/proforma = documentos soporte sin valor de factura.",
        },
        "emisor": {"type": "object", "properties": {
            "nombre": TXT, "nif": TXT, "direccion": TXT,
            "iban": {"type": ["string", "null"], "description": "IBAN para el pago, si figura."}},
            "required": ["nombre", "nif"]},
        "receptor": {"type": "object", "properties": {"nombre": TXT, "nif": TXT}, "required": ["nombre", "nif"]},
        "numero": {"type": ["string", "null"], "description": "Número de factura exactamente como aparece."},
        "fecha": {"type": ["string", "null"], "description": "Fecha de emisión en formato YYYY-MM-DD."},
        "fecha_vencimiento": {"type": ["string", "null"], "description": "YYYY-MM-DD, si figura."},
        "periodo": {"type": ["string", "null"], "description": "Mes/periodo facturado si se indica (p.ej. 'agosto 2026')."},
        "obra": {"type": "object", "properties": {
            "texto_literal": {"type": ["string", "null"], "description": "Copia literal de la referencia de obra/asunto/proyecto."},
            "codigo": {"type": ["string", "null"], "description": "Código de obra de la lista facilitada si lo reconoces."}},
            "required": ["texto_literal", "codigo"]},
        "pedido_contrato": TXT,
        "presupuesto_referencia": TXT,
        "albaranes": {"type": "array", "items": {"type": "string"}},
        "factura_rectificada": {"type": ["string", "null"], "description": "Número de la factura que se rectifica/abona."},
        "concepto_general": {"type": ["string", "null"], "description": "Resumen en una frase de lo facturado."},
        "lineas": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "codigo": TXT,
                "descripcion": {"type": "string"},
                "cantidad": NUM, "unidad": TXT, "precio_unitario": NUM,
                "descuento_pct": NUM,
                "importe": {**NUM, "description": "Importe neto de la línea TAL CUAL aparece (con signo)."},
                "tipo_linea": {"type": "string", "enum": ["normal", "extra", "descuento", "anticipo_deducido", "portes", "otro"]},
                "es_extra": {"type": "boolean", "description": "true si el documento lo marca como extra / fuera de presupuesto."},
                "albaran": TXT,
                "partida_codigo": {"type": ["string", "null"], "description": "Código de partida de la lista facilitada."},
                "partida_confianza": {"type": "number", "minimum": 0, "maximum": 1},
            }, "required": ["descripcion", "importe", "tipo_linea", "partida_codigo", "partida_confianza"]},
        },
        "importe_bruto": NUM,
        "base_imponible": NUM,
        "impuestos": {"type": "array", "items": {"type": "object", "properties": {
            "tipo_pct": NUM, "base": NUM, "cuota": NUM, "recargo_pct": NUM, "recargo_cuota": NUM},
            "required": ["tipo_pct", "base", "cuota"]}},
        "inversion_sujeto_pasivo": {"type": "boolean", "description": "true si menciona inversión del sujeto pasivo (art. 84.Uno.2º LIVA)."},
        "exencion_motivo": TXT,
        "total_factura": {**NUM, "description": "Total de la factura (base + IVA + recargo), antes de retenciones."},
        "irpf": {"type": "object", "properties": {"pct": NUM, "importe": NUM}},
        "retencion_garantia": {"type": "object", "properties": {"pct": NUM, "base": NUM, "importe": NUM}},
        "total_a_pagar": {**NUM, "description": "Líquido a pagar tras retenciones (IRPF, garantía)."},
        "forma_pago": TXT,
        "confianza": {"type": "number", "minimum": 0, "maximum": 1,
                      "description": "Confianza global en la extracción."},
        "campos_dudosos": {"type": "array", "items": {"type": "string"}},
        "observaciones": {"type": ["string", "null"], "description": "Cualquier cosa anómala que un contable deba saber."},
    },
    "required": ["tipo_documento", "emisor", "receptor", "numero", "fecha", "obra", "lineas",
                 "base_imponible", "impuestos", "inversion_sujeto_pasivo", "total_factura",
                 "total_a_pagar", "confianza", "campos_dudosos"],
}

SYSTEM = """Eres un técnico contable experto en facturación de empresas constructoras españolas.
Tu única tarea es TRANSCRIBIR con exactitud los datos de un documento (factura, abono, certificación,
albarán...) al esquema de la herramienta `registrar_documento`.

REGLAS ESTRICTAS
1. No inventes. Si un dato no figura, devuelve null. Nunca rellenes huecos con estimaciones.
2. No calcules totales: copia los importes EXACTAMENTE como están impresos (convertidos a formato
   máquina: punto decimal, sin separador de miles; '1.234,56 €' -> '1234.56').
3. Signos: los abonos, descuentos, deducciones de anticipos/entregas a cuenta y pronto pago van en negativo.
4. Líneas: incluye TODAS las líneas con importe, en orden. Si el documento deduce una entrega a cuenta o
   un anticipo antes de la base imponible, añádelo como línea negativa con tipo_linea='anticipo_deducido'.
   Si hay un descuento global antes de la base, línea negativa tipo 'descuento'. Las retenciones (IRPF,
   garantía) NO son líneas: van en sus campos.
   Si el documento solo trae un total sin desglose, crea una única línea con el concepto.
   Las líneas de texto sin importe (notas, mediciones auxiliares) NO se incluyen.
5. IVA: una entrada por tipo impositivo. Si hay inversión del sujeto pasivo o exención, IVA = 0 con su base.
6. Retención de garantía (habitual 5 % en subcontratas de obra) e IRPF (profesionales) por separado. Indica la
   base sobre la que se calcula si difiere de la base imponible.
7. Obra: copia el texto literal de la referencia de obra/asunto/proyecto; si coincide con algún código de la
   lista de obras facilitada, pon ese código.
8. Partidas: asigna a cada línea el código de partida más adecuado de la lista facilitada y tu confianza (0-1).
   Si no encaja claramente en ninguna, usa '99'.
9. Identifica el EMISOR (quien factura) y el RECEPTOR (a quien se factura). Cuidado: en muchas facturas el
   receptor aparece arriba a la derecha.
10. Si el PDF contiene varios documentos (factura + albaranes), extrae la FACTURA y lista los albaranes.
11. En campos_dudosos lista los campos que no se leen bien o son ambiguos. Sé honesto con la confianza."""


def _user_prompt(obras: list[dict], partidas: list[dict]) -> str:
    lo = "\n".join(f"- {o['codigo']}: {o['nombre']} (alias: {o.get('alias') or '-'})" for o in obras) or "- (ninguna)"
    lp = "\n".join(f"- {p['codigo']}: {p['descripcion']}" for p in partidas) or "- 99: Sin asignar"
    return (f"OBRAS DADAS DE ALTA:\n{lo}\n\nPARTIDAS (capítulos de coste):\n{lp}\n\n"
            "Transcribe el documento adjunto con la herramienta registrar_documento.")


class ExtractionError(Exception):
    pass


def extraer_documento(pdf_bytes: bytes, api_key: str, model: str, obras: list[dict], partidas: list[dict],
                      max_tokens: int = 16000) -> dict:
    """Devuelve {'data': dict, 'modelo', 'tokens_entrada', 'tokens_salida', 'segundos'}."""
    import anthropic

    if not api_key:
        raise ExtractionError("Falta la API key de Anthropic (Configuración).")
    client = anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=600)
    t0 = time.time()
    content = [
        {"type": "document",
         "source": {"type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode()}},
        {"type": "text", "text": _user_prompt(obras, partidas)},
    ]
    try:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            system=SYSTEM,
            tools=[{"name": "registrar_documento",
                    "description": "Registra los datos transcritos del documento.",
                    "input_schema": SCHEMA}],
            tool_choice={"type": "tool", "name": "registrar_documento"},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIStatusError as e:
        raise ExtractionError(f"Error de la API ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ExtractionError(f"No se pudo conectar con la API: {e}") from e

    data = None
    for block in msg.content:
        if block.type == "tool_use" and block.name == "registrar_documento":
            data = block.input
    if data is None:
        raise ExtractionError("La IA no devolvió datos estructurados.")
    if msg.stop_reason == "max_tokens":
        raise ExtractionError("Documento demasiado largo: la respuesta se cortó. Divide el PDF o sube max_tokens.")
    if isinstance(data, str):
        data = json.loads(data)
    return {"data": data, "modelo": model,
            "tokens_entrada": msg.usage.input_tokens, "tokens_salida": msg.usage.output_tokens,
            "segundos": round(time.time() - t0, 1)}
