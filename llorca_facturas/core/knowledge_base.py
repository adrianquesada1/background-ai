"""
Base de conocimiento LOCAL de LLORCA.

No usa APIs. Guarda ejemplos aprobados y los recupera por similitud semántica
(Qwen3 Embedding vía Ollama) y, si no hay embedding disponible, por coincidencia
léxica. SQLite es la fuente de verdad; Chroma es un índice acelerador opcional.

La idea no es reentrenar el LLM después de cada factura: cada corrección/aprobación
se convierte en un ejemplo recuperable para las siguientes facturas.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import requests

from . import db
from .config import DATA_DIR

KB_DIR = DATA_DIR / "knowledge"
CHROMA_DIR = KB_DIR / "chroma"
KB_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_memories (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER UNIQUE,
    scope TEXT NOT NULL DEFAULT 'global',
    scope_key TEXT DEFAULT '',
    texto TEXT NOT NULL,
    metadata TEXT,
    embedding TEXT,
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
    actualizado_en TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ai_mem_scope ON ai_memories(scope, scope_key);
"""

_CHROMA = None
_COLLECTION = None


def init(con) -> None:
    con.executescript(SCHEMA)
    con.commit()


def _chroma_collection():
    global _CHROMA, _COLLECTION
    if _COLLECTION is not None:
        return _COLLECTION
    try:
        import chromadb
        _CHROMA = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _COLLECTION = _CHROMA.get_or_create_collection(
            name="llorca_facturas",
            metadata={"hnsw:space": "cosine"},
        )
        return _COLLECTION
    except Exception:
        _CHROMA = False
        _COLLECTION = False
        return None


def _embed(texto: str, url: str, model: str, timeout: int = 8) -> list[float] | None:
    if not texto or not model:
        return None
    try:
        r = requests.post(url.rstrip("/") + "/api/embed", json={"model": model, "input": texto[:30000]}, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("embeddings") or []
        return data[0] if data else None
    except Exception:
        return None


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else -1.0


def _tokens(texto: str) -> set[str]:
    return {x for x in re.findall(r"[a-záéíóúüñ0-9]{3,}", (texto or "").lower()) if x not in {
        "para", "como", "esta", "este", "desde", "hasta", "factura", "importe", "total", "fecha", "obra"
    }}


def _doc_text(d: dict, lineas: list[dict] | None = None) -> str:
    partes = [
        f"Proveedor: {d.get('emisor_nombre') or ''}",
        f"NIF proveedor: {d.get('emisor_nif') or ''}",
        f"Obra: {d.get('obra_codigo') or ''} {d.get('obra_nombre') or ''}",
        f"Referencia obra: {d.get('referencia_obra_texto') or ''}",
        f"Concepto: {d.get('concepto_general') or ''}",
        f"Tipo: {d.get('tipo_documento') or ''}",
        f"Cuenta: {d.get('cuenta_contable') or ''}",
        f"Base: {d.get('base_imponible_cents') or 0} céntimos",
        f"Total: {d.get('total_factura_cents') or 0} céntimos",
        f"Retención garantía: {d.get('ret_garantia_pct') or ''}% / {d.get('ret_garantia_cents') or 0} céntimos",
        f"IRPF: {d.get('irpf_pct') or ''}% / {d.get('irpf_cents') or 0} céntimos",
        f"Total a pagar: {d.get('total_a_pagar_cents') or 0} céntimos",
    ]
    for l in (lineas or [])[:40]:
        partes.append(f"Línea: {l.get('codigo') or ''} {l.get('descripcion') or ''} -> {l.get('importe_cents') or 0} céntimos")
    return "\n".join(x for x in partes if x.strip())


def index_document(con, doc_id: int, ollama_url: str = "http://localhost:11434",
                   embedding_model: str = "qwen3-embedding:0.6b") -> bool:
    """Indexa un documento aprobado. Nunca lanza una excepción hacia la UI."""
    init(con)
    d = db.one(con, """
        SELECT d.*, o.codigo AS obra_codigo, o.nombre AS obra_nombre
        FROM documentos d LEFT JOIN obras o ON o.id=d.obra_id WHERE d.id=?
    """, (doc_id,))
    if not d:
        return False
    lineas = db.rows(con, "SELECT codigo, descripcion, importe_cents FROM lineas WHERE documento_id=? ORDER BY orden", (doc_id,))
    texto = _doc_text(d, lineas)
    scope_key = f"{d.get('emisor_nif') or ''}|{d.get('obra_id') or ''}"
    metadata = {
        "doc_id": doc_id,
        "proveedor_nif": d.get("emisor_nif") or "",
        "proveedor": d.get("emisor_nombre") or "",
        "obra_id": d.get("obra_id") or 0,
        "obra": d.get("obra_codigo") or "",
        "numero": d.get("numero") or "",
        "fecha": d.get("fecha") or "",
        "estado": d.get("estado") or "",
    }
    emb = _embed(texto, ollama_url, embedding_model)
    with db.tx(con):
        old = db.one(con, "SELECT id FROM ai_memories WHERE doc_id=?", (doc_id,))
        if old:
            con.execute("UPDATE ai_memories SET scope=?, scope_key=?, texto=?, metadata=?, embedding=?, actualizado_en=? WHERE id=?",
                        ("proveedor_obra", scope_key, texto, json.dumps(metadata, ensure_ascii=False),
                         json.dumps(emb) if emb else None, db.now_iso(), old["id"]))
            mem_id = old["id"]
        else:
            cur = con.execute("INSERT INTO ai_memories(scope, scope_key, texto, metadata, embedding, creado_en, actualizado_en) VALUES(?,?,?,?,?,?,?)",
                              ("proveedor_obra", scope_key, texto, json.dumps(metadata, ensure_ascii=False),
                               json.dumps(emb) if emb else None, db.now_iso(), db.now_iso()))
            mem_id = cur.lastrowid
    col = _chroma_collection()
    if col is not None and emb:
        try:
            col.upsert(ids=[str(mem_id)], embeddings=[emb], documents=[texto], metadatas=[metadata])
        except Exception:
            pass
    return True


def index_approved(con, ollama_url: str, embedding_model: str) -> int:
    init(con)
    ids = [r["id"] for r in db.rows(con, "SELECT id FROM documentos WHERE estado='aprobada' ORDER BY id")]
    return sum(index_document(con, i, ollama_url, embedding_model) for i in ids)


def search(con, query: str, ollama_url: str = "http://localhost:11434",
           embedding_model: str = "qwen3-embedding:0.6b", top_k: int = 5) -> list[dict]:
    init(con)
    query = (query or "").strip()
    if not query:
        return []
    qemb = _embed(query, ollama_url, embedding_model)
    col = _chroma_collection()
    if col is not None and qemb:
        try:
            res = col.query(query_embeddings=[qemb], n_results=top_k, include=["documents", "metadatas", "distances"])
            out = []
            for text, meta, dist in zip(res.get("documents", [[]])[0], res.get("metadatas", [[]])[0], res.get("distances", [[]])[0]):
                out.append({"texto": text, "metadata": meta, "score": round(1 - float(dist), 4)})
            if out:
                return out
        except Exception:
            pass

    rows = db.rows(con, "SELECT * FROM ai_memories ORDER BY id DESC")
    qt = _tokens(query)
    scored = []
    for r in rows:
        meta = json.loads(r["metadata"] or "{}")
        emb = json.loads(r["embedding"]) if r.get("embedding") else None
        sem = _cos(qemb, emb) if qemb and emb else -1
        overlap = len(qt & _tokens(r["texto"])) / max(1, len(qt))
        score = max(sem, overlap * 0.65)
        scored.append((score, r, meta))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"texto": r["texto"], "metadata": meta, "score": round(float(score), 4)}
            for score, r, meta in scored[:top_k] if score > 0]


def contexto(con, query: str, ollama_url: str = "http://localhost:11434",
             embedding_model: str = "qwen3-embedding:0.6b", top_k: int = 4, max_chars: int = 7000) -> str:
    hits = search(con, query, ollama_url, embedding_model, top_k)
    if not hits:
        return ""
    chunks = []
    for i, h in enumerate(hits, 1):
        m = h["metadata"]
        chunks.append(f"EJEMPLO VALIDADO {i} (similitud {h['score']:.2f})\n"
                      f"Proveedor: {m.get('proveedor','')} · NIF: {m.get('proveedor_nif','')} · "
                      f"Obra: {m.get('obra','')} · Factura: {m.get('numero','')} · Fecha: {m.get('fecha','')}\n"
                      f"{h['texto']}")
    return "\n\n".join(chunks)[:max_chars]
