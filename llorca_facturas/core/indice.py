"""
Búsqueda de texto completo dentro de todos los documentos (PDF con texto y escaneados tras el OCR), página a página.

Usa el índice FTS5 de SQLite (incluido en Python): búsquedas instantáneas aunque haya decenas de miles de páginas,
sin distinguir mayúsculas ni tildes («albaran» encuentra «ALBARÁN»). Si FTS5 no estuviera disponible, se busca con LIKE.
"""
from __future__ import annotations

import io
import re

from . import db

_FTS = None


def init(con) -> bool:
    global _FTS
    con.execute("CREATE TABLE IF NOT EXISTS doc_paginas (documento_id INTEGER NOT NULL, pagina INTEGER NOT NULL, texto TEXT, "
                "PRIMARY KEY (documento_id, pagina))")
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(texto, documento_id UNINDEXED, pagina UNINDEXED, "
                    "tokenize='unicode61 remove_diacritics 2')")
        _FTS = True
    except Exception:
        _FTS = False
    con.commit()
    return _FTS


def paginas_pdf(data: bytes) -> list[str]:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return [(p.extract_text() or "") for p in pdf.pages[:400]]
    except Exception:
        return []


def indexar(con, doc_id: int, paginas: list[str]) -> None:
    if _FTS is None:
        init(con)
    con.execute("DELETE FROM doc_paginas WHERE documento_id=?", (doc_id,))
    if _FTS:
        con.execute("DELETE FROM doc_fts WHERE documento_id=?", (doc_id,))
    for i, t in enumerate(paginas, start=1):
        t = t or ""
        con.execute("INSERT OR REPLACE INTO doc_paginas (documento_id, pagina, texto) VALUES (?,?,?)", (doc_id, i, t))
        if _FTS and t.strip():
            con.execute("INSERT INTO doc_fts (texto, documento_id, pagina) VALUES (?,?,?)", (t, doc_id, i))
    con.commit()


def indexar_documento(con, doc_id: int) -> int:
    """Indexa desde el PDF (capa de texto) o, si es escaneado, desde el texto OCR guardado (páginas separadas por \\f)."""
    d = db.one(con, "SELECT file_path, texto, tiene_texto FROM documentos WHERE id=?", (doc_id,))
    if not d:
        return 0
    pags = []
    if d["file_path"]:
        try:
            pags = paginas_pdf(open(d["file_path"], "rb").read())
        except Exception:
            pags = []
    if not any(p.strip() for p in pags) and d["texto"]:
        pags = d["texto"].split("\f") if "\f" in d["texto"] else [d["texto"]]
    indexar(con, doc_id, pags)
    return len(pags)


def pendientes_de_indexar(con) -> list[int]:
    return [r["id"] for r in db.rows(con, """SELECT id FROM documentos WHERE id NOT IN (SELECT DISTINCT documento_id FROM doc_paginas)
                                             AND estado<>'eliminado'""")]


def _consulta_fts(q: str) -> str:
    """Texto del usuario -> consulta FTS segura: palabras (con prefijo) y «frases entre comillas»."""
    frases = re.findall(r'"([^"]+)"', q)
    resto = re.sub(r'"[^"]+"', " ", q)
    palabras = [w for w in re.findall(r"[\wÀ-ÿ./-]+", resto) if len(w) >= 2]
    partes = [f'"{f}"' for f in frases] + [f'"{w}"*' if not re.search(r"[./-]", w) else f'"{w}"' for w in palabras]
    return " AND ".join(partes)


def buscar(con, q: str, limite: int = 200, ids_permitidos: set | None = None) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    if _FTS is None:
        init(con)
    filas = []
    if _FTS:
        try:
            filas = db.rows(con, """SELECT f.documento_id, f.pagina, snippet(doc_fts, 0, '«', '»', ' … ', 14) AS fragmento, bm25(doc_fts) AS rel
                                    FROM doc_fts f WHERE doc_fts MATCH ? ORDER BY rel LIMIT ?""", (_consulta_fts(q), limite))
        except Exception:
            filas = []
    if not filas:
        like = f"%{q}%"
        for r in db.rows(con, "SELECT documento_id, pagina, texto FROM doc_paginas WHERE texto LIKE ? LIMIT ?", (like, limite)):
            i = r["texto"].lower().find(q.lower())
            frag = r["texto"][max(0, i - 60):i + len(q) + 60].replace("\n", " ") if i >= 0 else ""
            filas.append({"documento_id": r["documento_id"], "pagina": r["pagina"], "fragmento": frag, "rel": 0})
    if ids_permitidos is not None:
        filas = [f for f in filas if f["documento_id"] in ids_permitidos]
    if not filas:
        return []
    docs = {d["id"]: d for d in db.rows(con, f"""SELECT d.id, d.filename, d.emisor_nombre, d.numero, d.fecha, d.estado, o.codigo AS obra
                                                 FROM documentos d LEFT JOIN obras o ON o.id=d.obra_id
                                                 WHERE d.id IN ({','.join('?' * len(filas))})""", [f["documento_id"] for f in filas])}
    out = []
    for f in filas:
        d = docs.get(f["documento_id"])
        if d and d["estado"] != "eliminado":
            out.append({**f, **{k: d[k] for k in ("filename", "emisor_nombre", "numero", "fecha", "obra")},
                        "fragmento": re.sub(r"\s+", " ", f["fragmento"] or "")})
    return out
