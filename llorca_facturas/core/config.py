"""Configuración global de la aplicación."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LLORCA_DATA_DIR", BASE_DIR / "data"))
PDF_DIR = DATA_DIR / "pdfs"
DB_PATH = DATA_DIR / "llorca_facturas.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

# Empresa receptora (se valida que cada factura venga a nombre de ella)
EMPRESA_NOMBRE = "LLORCA GROUP HISPANIA, S.L."
EMPRESA_NIF = "B54727722"

# Modelos Claude seleccionables (el primero es el predeterminado)
MODELOS_OLLAMA = {
    "vision": "qwen3-vl:4b-instruct",
    "razonamiento": "qwen3:4b-instruct",
    "embeddings": "qwen3-embedding:0.6b",
}

MODELOS_CLAUDE = [
    "claude-sonnet-5",
    "claude-opus-5-5",
    "claude-haiku-4-5-20251001",
]

# Tolerancias de cuadre (en céntimos). Justificación en core/validation.py
TOL_CUADRE_CENTS = 2          # sumas de totales (base + IVA = total, etc.)
TOL_IVA_CENTS = 1             # cuota = base x tipo, redondeo a céntimo

# Días para la liberación de la retención de garantía (habitual: 1 año desde recepción)
DIAS_GARANTIA = 365

TIPOS_DOCUMENTO = [
    "factura", "abono", "anticipo", "proforma", "certificacion",
    "albaran", "parte_horas", "presupuesto", "otro",
]
# Solo estos tipos computan como coste real de obra
TIPOS_COMPUTABLES = ("factura", "abono", "anticipo")

ESTADOS = ["sin_procesar", "pendiente_revision", "revisada", "aprobada", "rechazada", "duplicado", "eliminado"]
ESTADO_LABEL = {
    "sin_procesar": "Sin procesar (falta lectura IA)",
    "pendiente_revision": "Pendiente de revisión",
    "revisada": "Revisada",
    "aprobada": "Aprobada",
    "rechazada": "Rechazada",
    "duplicado": "Duplicado (no computa)",
    "eliminado": "En papelera",
}

TIPOS_LINEA = ["normal", "extra", "descuento", "anticipo_deducido", "portes", "otro"]

SEVERIDADES = {"critica": 0, "alta": 1, "media": 2, "info": 3}
SEVERIDAD_ICONO = {"critica": ":red[:material/error:]", "alta": ":orange[:material/warning:]", "media": ":orange[:material/info:]", "info": ":gray[:material/info:]"}
SEVERIDAD_TEXTO = {"critica": "Crítica", "alta": "Alta", "media": "Media", "info": "Informativa"}
