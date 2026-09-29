"""
Punto único de lectura local de documentos.

Pipeline por defecto:
  1. texto nativo del PDF / OCR
  2. reglas y solver matemático
  3. memoria local (RAG) de facturas aprobadas
  4. Qwen3-VL para documentos/visón cuando hace falta
  5. Qwen3 para razonamiento textual opcional
  6. validación determinista

Todo se ejecuta en el equipo. No se requiere ninguna API.
"""
from __future__ import annotations

from . import db
from .config import MODELOS_OLLAMA

MOTORES = {
    "local_auto": "Local AI Agent: Qwen3-VL + OCR/reglas + memoria local",
    "local_reglas": "Local: OCR + reglas matemáticas (sin modelo de IA)",
    "claude": "Nube: API de Claude (opcional)",
}


def config(con) -> dict:
    return {
        "motor": db.get_setting(con, "motor_lectura", "local_auto"),
        "ollama_url": db.get_setting(con, "ollama_url", "http://localhost:11434"),
        "ollama_modelo": db.get_setting(con, "ollama_modelo", MODELOS_OLLAMA["vision"]),
        "ollama_vision_modelo": db.get_setting(con, "ollama_vision_modelo", MODELOS_OLLAMA["vision"]),
        "ollama_razonamiento_modelo": db.get_setting(con, "ollama_razonamiento_modelo", MODELOS_OLLAMA["razonamiento"]),
        "ollama_embedding_modelo": db.get_setting(con, "ollama_embedding_modelo", MODELOS_OLLAMA["embeddings"]),
        "ocr": db.get_setting(con, "ocr", "1") == "1",
        "usar_gpu": db.get_setting(con, "ollama_gpu", "0") == "1",
        "num_ctx": int(db.get_setting(con, "ollama_ctx", "8192") or 8192),
        "ia_siempre": db.get_setting(con, "ia_siempre", "0") == "1",
        "timeout_ia": int(db.get_setting(con, "timeout_ia", "420") or 420),
        "memoria": db.get_setting(con, "kb_memoria", "1") == "1",
    }


def opciones_ollama(cfg: dict) -> dict:
    # En equipos modestos, CPU + contexto contenido es mucho más estable que forzar una GPU pequeña.
    o = {"num_ctx": min(int(cfg.get("num_ctx", 8192)), 8192), "num_predict": 900}
    if not cfg.get("usar_gpu"):
        o["num_gpu"] = 0
    return o


def necesita_api(con) -> bool:
    return config(con)["motor"] == "claude"


def leer(con, data: bytes, obras: list[dict], partidas: list[dict], api_key: str = "", modelo_claude: str = "",
         cfg: dict | None = None, proveedores: dict | None = None, filename: str = "") -> dict:
    cfg = cfg or config(con)
    if cfg["motor"] == "claude":
        from .extractor import extraer_documento
        return extraer_documento(data, api_key, modelo_claude, obras, partidas)

    from .extractor_local import extraer_local, EMPRESA_NIFS
    try:
        from .validation import sociedades
        EMPRESA_NIFS.update(sociedades(con).keys())
    except Exception:
        pass
    if proveedores is None:
        proveedores = {r["nif"]: r["nombre"] for r in db.rows(con, "SELECT nif, nombre FROM proveedores WHERE nif IS NOT NULL")}
    from . import plantillas as _pl
    from . import knowledge_base as _kb
    _pl.init(con)
    _kb.init(con)
    memoria = ""
    if cfg.get("memoria") and cfg["motor"] != "local_reglas":
        # Para no enviar el documento completo al índice, usamos el texto disponible y nombres de obras.
        from .pdf_utils import read_text
        try:
            txt, _ = read_text(data)
        except Exception:
            txt = ""
        query = txt[:10000]
        memoria = _kb.contexto(con, query, cfg["ollama_url"], cfg["ollama_embedding_modelo"], top_k=4)

    return extraer_local(
        data, obras, partidas, proveedores,
        plantillas=_pl.cargar(con),
        memoria=memoria,
        motor="reglas" if cfg["motor"] == "local_reglas" else "auto",
        ollama_url=cfg["ollama_url"],
        ollama_modelo=cfg["ollama_vision_modelo"],
        usar_ocr=cfg["ocr"], filename=filename,
        ollama_opciones=opciones_ollama(cfg),
        ia_siempre=cfg.get("ia_siempre", False),
        timeout_ia=cfg.get("timeout_ia", 420),
    )
