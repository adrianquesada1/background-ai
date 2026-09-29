"""
Cliente robusto para la IA local (Ollama).

Por qué fallaba con «500 Internal Server Error»:
- En equipos con una GPU pequeña (p. ej. GT 1030 de 2 GB) Ollama intenta cargar parte del modelo en la
  gráfica, se queda sin memoria y el proceso del modelo cae -> error 500.
- Con un contexto muy grande (16.384 tokens) el modelo de 7B necesita bastante más RAM.

Solución aplicada:
1. Por defecto se ejecuta SOLO en CPU (num_gpu = 0) y con contexto de 8.192 tokens.
2. Si aun así falla, se reintenta automáticamente con menos contexto y, en extracción, con formato JSON simple.
3. Siempre se muestra el mensaje REAL de Ollama (no solo «500»), para saber qué pasa.
"""
from __future__ import annotations

import json
import time

import requests

DEFAULTS = {"num_ctx": 8192, "num_gpu": 0, "temperature": 0, "num_thread": 0}

# Modelos locales recomendados para LLORCA. Se elige el primero que esté instalado.
PREFERENCIA = (
    "qwen3-vl:4b-instruct", "qwen3-vl:2b-instruct", "qwen3-vl:4b", "qwen3-vl:2b",
    "qwen2.5vl:7b", "qwen2.5vl:3b", "gemma4:e4b", "gemma3:4b", "llama3.2-vision",
)
PREFERENCIA_RAZONAMIENTO = ("qwen3:4b-instruct", "qwen3:4b", "qwen3:8b-instruct", "qwen3:8b", "gemma4:e4b", "llama3.2:3b")


class OllamaError(Exception):
    pass


def _post(url: str, payload: dict, timeout: int) -> dict:
    r = requests.post(url.rstrip("/") + "/api/chat", json=payload, timeout=timeout)
    if r.status_code >= 400:
        try:
            detalle = r.json().get("error", r.text)
        except Exception:
            detalle = r.text
        raise OllamaError(f"Ollama respondió {r.status_code}: {detalle[:400]}")
    return r.json()


def chat(url: str, modelo: str, mensajes: list[dict], formato=None, tools=None, opciones: dict | None = None,
         timeout: int = 900) -> dict:
    """
    Llamada a /api/chat con reintentos progresivamente más conservadores.
    Devuelve el 'message' de Ollama.
    """
    base = {**DEFAULTS, **(opciones or {})}
    intentos = [
        {"options": base, "format": formato},
        {"options": {**base, "num_gpu": 0, "num_ctx": min(base["num_ctx"], 6144)}, "format": formato},
        {"options": {**base, "num_gpu": 0, "num_ctx": 4096}, "format": "json" if formato else None},
    ]
    ultimo = None
    for it in intentos:
        payload = {"model": modelo, "messages": mensajes, "stream": False, "keep_alive": "5m", "options": it["options"]}
        if it["format"] is not None:
            payload["format"] = it["format"]
        if tools:
            payload["tools"] = tools
        if "qwen3" in (modelo or "").lower():
            payload["think"] = False
        try:
            return _post(url, payload, timeout)["message"]
        except requests.exceptions.ConnectionError as e:
            raise OllamaError("No se puede conectar con Ollama. ¿Está abierto? (icono de la llama junto al reloj)") from e
        except requests.exceptions.Timeout as e:
            raise OllamaError("La IA local ha tardado demasiado en responder.") from e
        except OllamaError as e:
            ultimo = e
            time.sleep(1)
    raise ultimo


def modelos(url: str) -> list[str]:
    try:
        r = requests.get(url.rstrip("/") + "/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def diagnostico(url: str, modelo: str, opciones: dict | None = None) -> list[tuple[bool, str]]:
    """Comprobaciones paso a paso, con la causa real si algo falla."""
    out = []
    ms = modelos(url)
    if not ms and not _vivo(url):
        return [(False, "Ollama no responde en " + url + ". Ábrelo desde el menú Inicio o ejecuta INICIAR.")]
    out.append((True, "Servidor Ollama activo"))
    if modelo not in ms:
        out.append((False, f"El modelo «{modelo}» no está instalado. Instalados: {', '.join(ms) or 'ninguno'}. "
                           f"Ejecuta: ollama pull {modelo}"))
        return out
    out.append((True, f"Modelo «{modelo}» instalado"))
    t0 = time.time()
    try:
        m = chat(url, modelo, [{"role": "user", "content": 'Responde solo con este JSON: {"ok": true}'}],
                 formato="json", opciones=opciones, timeout=600)
        seg = time.time() - t0
        ok = '"ok"' in (m.get("content") or "")
        out.append((ok, f"Respuesta de prueba en {seg:.0f} s" + ("" if ok else f": respuesta inesperada «{m.get('content', '')[:80]}»")))
    except OllamaError as e:
        out.append((False, str(e)))
        txt = str(e).lower()
        if "memory" in txt or "cuda" in txt or "terminated" in txt:
            out.append((False, "Causa probable: falta de memoria. Cierra programas, deja «Usar GPU» desactivado o prueba "
                               "un modelo Vision más pequeño (qwen3-vl:2b-instruct)."))
    return out


def embedding(url: str, modelo: str, texto: str, timeout: int = 90) -> list[float]:
    """Embedding local de Ollama. Devuelve [] si el modelo no está instalado/disponible."""
    try:
        r = requests.post(url.rstrip("/") + "/api/embed", json={"model": modelo, "input": texto[:30000]}, timeout=timeout)
        r.raise_for_status()
        return (r.json().get("embeddings") or [[]])[0]
    except Exception:
        return []



def _vivo(url: str) -> bool:
    try:
        requests.get(url.rstrip("/") + "/api/version", timeout=3)
        return True
    except Exception:
        return False


def parse_json(texto: str) -> dict:
    """Extrae el primer objeto JSON de una respuesta (por si el modelo añade texto)."""
    texto = (texto or "").strip()
    try:
        return json.loads(texto)
    except Exception:
        i, j = texto.find("{"), texto.rfind("}")
        if i >= 0 and j > i:
            return json.loads(texto[i:j + 1])
        raise




def elegir_modelo(url: str, configurado: str) -> str | None:
    """El configurado si está instalado; si no, el mejor instalado según preferencia (evita fallos por nombre)."""
    ms = modelos(url)
    if configurado in ms:
        return configurado
    for p in PREFERENCIA:
        for m in ms:
            if m == p or m.startswith(p.split(":")[0] + ":"):
                return m
    # Si no hay un vision model, devuelve cualquier modelo instalado para que la UI pueda diagnosticarlo.
    return ms[0] if ms else None


def elegir_modelo_razonamiento(url: str, configurado: str) -> str | None:
    ms = modelos(url)
    if configurado in ms:
        return configurado
    for p in PREFERENCIA_RAZONAMIENTO:
        for m in ms:
            if m == p or m.startswith(p.split(":")[0] + ":"):
                return m
    return ms[0] if ms else None


_CAPS: dict = {}


def capacidades(url: str, modelo: str) -> set[str]:
    """Qué sabe hacer el modelo instalado (tools, vision, thinking…). Evita llamadas que van a fallar."""
    k = (url, modelo)
    if k not in _CAPS:
        try:
            r = requests.post(url.rstrip("/") + "/api/show", json={"model": modelo}, timeout=10)
            _CAPS[k] = set(r.json().get("capabilities") or [])
        except Exception:
            _CAPS[k] = set()
    return _CAPS[k]
