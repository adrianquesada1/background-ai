"""Compatibilidad del extractor local.

El proyecto importa `core.extractor_local`, pero la implementación consolidada
vive en `core.extractor`. Este módulo reexporta esa implementación y evita
duplicar o mantener dos extractores diferentes.
"""

from .extractor import (
    EMPRESA_NIFS,
    ExtractionError,
    extraer_local,
    ollama_disponible,
    motor_ollama,
    motor_reglas,
    fusionar,
    huecos,
    necesita_ia,
    obtener_texto,
    ocr_pdf,
    _valor_columna,
)

__all__ = [
    "EMPRESA_NIFS",
    "ExtractionError",
    "extraer_local",
    "ollama_disponible",
    "motor_ollama",
    "motor_reglas",
    "fusionar",
    "huecos",
    "necesita_ia",
    "obtener_texto",
    "ocr_pdf",
    "_valor_columna",
]
