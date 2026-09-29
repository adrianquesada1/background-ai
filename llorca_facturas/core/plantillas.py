"""
Aprendizaje por proveedor («plantillas»).

Hay cientos de formatos de factura. En lugar de intentar prever todos, la aplicación APRENDE de cada factura aprobada:
anota junto a qué etiqueta aparecían el número, la fecha y los importes («Nº Doc.», «Fecha expedición», «Líquido»…),
tanto en la misma línea como en la cabecera de columna. La próxima factura del mismo proveedor se lee buscando primero
esas etiquetas. Así cada proveedor nuevo solo hay que corregirlo una vez.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from . import db
from .money import amount_variants, from_cents, parse_amount, q2

SCHEMA = """
CREATE TABLE IF NOT EXISTS plantillas (
    nif TEXT NOT NULL, campo TEXT NOT NULL, etiqueta TEXT NOT NULL, modo TEXT NOT NULL,
    veces INTEGER DEFAULT 1, ultima TEXT,
    PRIMARY KEY (nif, campo, etiqueta, modo)
);
"""
CAMPOS = ("numero", "fecha", "base_imponible", "total_factura", "total_a_pagar")


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def _etiqueta_izq(linea: str, pos: int) -> str | None:
    pal = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñºª.]{2,}", linea[:pos])[-3:]
    et = " ".join(pal).strip(" .:").lower()
    return et if len(et) >= 2 else None


def _etiqueta_arriba(lineas: list[str], i: int, c0: int, c1: int) -> str | None:
    """Cabecera de columna: en las 4 líneas de encima, la primera que sea de etiquetas (sin cifras) y, en ella, la palabra
    más centrada sobre el valor (más la palabra pegada a su izquierda si es corta: «B. IMPONIBLE», «Nº FACTURA»)."""
    centro = (c0 + c1) / 2
    for j in range(i - 1, max(-1, i - 5), -1):
        ln = lineas[j]
        pals = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[^\s]+", ln)]
        cerca = [p for p in pals if p[0] <= c1 + 8 and p[1] >= c0 - 8]
        if not cerca:
            continue
        if any(re.search(r"\d", p[2]) for p in cerca):
            continue                                   # línea de datos, no de etiquetas
        k = min(range(len(pals)), key=lambda x: abs((pals[x][0] + pals[x][1]) / 2 - centro) if pals[x] in cerca else 10 ** 6)
        et = pals[k][2]
        if k > 0 and pals[k][0] - pals[k - 1][1] <= 2 and len(pals[k - 1][2]) <= 4:
            et = pals[k - 1][2] + " " + et
        et = re.sub(r"[^\wºª. ]", "", et).strip(" .").lower()
        return et or None
    return None


def _variantes(campo: str, valor) -> list[str]:
    if campo == "numero":
        return [str(valor)]
    if campo == "fecha":
        d = datetime.strptime(valor, "%Y-%m-%d")
        return [d.strftime(f) for f in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%-d/%-m/%Y", "%Y-%m-%d")]
    return sorted(amount_variants(from_cents(valor)), key=len, reverse=True)


def aprender(con, doc_id: int) -> int:
    d = db.one(con, "SELECT * FROM documentos WHERE id=?", (doc_id,))
    if not d or not d["emisor_nif"] or not d["texto"]:
        return 0
    lineas = d["texto"].splitlines()
    n = 0
    for campo in CAMPOS:
        col = {"base_imponible": "base_imponible_cents", "total_factura": "total_factura_cents",
               "total_a_pagar": "total_a_pagar_cents"}.get(campo, campo)
        valor = d.get(col)
        if valor in (None, "", 0):
            continue
        try:
            vars_ = _variantes(campo, valor)
        except Exception:
            continue
        hallado = None
        es_importe = campo not in ("numero", "fecha")
        # los importes totales están al final: se buscan de abajo arriba y solo con etiqueta en la misma línea
        orden = list(enumerate(lineas))[::-1] if es_importe else list(enumerate(lineas))
        for i, ln in orden:
            for v in vars_:
                p = ln.find(v)
                if p >= 0 and (campo != "numero" or re.search(r"\w", v)):
                    izq = _etiqueta_izq(ln, p)
                    if izq:
                        hallado = (izq, "linea")
                    elif es_importe:
                        pass
                    else:
                        arr = _etiqueta_arriba(lineas, i, p, p + len(v))
                        if arr:
                            hallado = (arr, "columna")
                    break
            if hallado:
                break
        if hallado:
            con.execute("INSERT INTO plantillas (nif, campo, etiqueta, modo, veces, ultima) VALUES (?,?,?,?,1,?) "
                        "ON CONFLICT(nif, campo, etiqueta, modo) DO UPDATE SET veces=veces+1, ultima=excluded.ultima",
                        (d["emisor_nif"], campo, hallado[0], hallado[1], db.now_iso()))
            n += 1
    con.commit()
    return n


def cargar(con) -> dict:
    out = defaultdict(lambda: defaultdict(list))
    for r in db.rows(con, "SELECT * FROM plantillas ORDER BY veces DESC"):
        out[r["nif"]][r["campo"]].append((r["etiqueta"], r["modo"]))
    return {k: dict(v) for k, v in out.items()}


PATRON = {
    "numero": r"[A-Z0-9][A-Z0-9/\-_.]*\d[A-Z0-9/\-_.]*",
    "fecha": r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|20\d{2}-\d{2}-\d{2}",
    "importe": r"-?(?:\d{1,3}(?:[.,]\d{3})+|\d+)[.,]\d{2}(?!\d)",
}


def aplicar(texto: str, reglas_proveedor: dict) -> dict:
    """Valores encontrados con las etiquetas aprendidas para este proveedor."""
    from .extractor_local import _valor_columna
    res = {}
    for campo, etiquetas in reglas_proveedor.items():
        pat = PATRON["numero"] if campo == "numero" else PATRON["fecha"] if campo == "fecha" else PATRON["importe"]
        for et, modo in etiquetas:
            et_re = r"\s*".join(re.escape(w) for w in et.split())
            v = None
            if modo == "linea":
                m = re.search(et_re + r"[\s:.#]*(" + pat + r")", texto, re.I)
                v = m.group(1) if m else None
            else:
                v = _valor_columna(texto, et_re, pat)
            if v:
                res[campo] = v
                break
    return res
