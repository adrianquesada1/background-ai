"""
Control económico de obra: CERTIFICACIÓN (venta) ↔ FACTURAS (coste) ↔ OFERTAS/CONTRATOS (compromiso).

Unidades:
- Certificación: milésimas de euro (enteros, sufijo _m), porque el documento trae 3 decimales.
- Facturas: céntimos (sufijo _cents). Conversión exacta: 1 céntimo = 10 milésimas.
Todas las sumas se hacen en enteros; Decimal solo para presentar.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from rapidfuzz import fuzz, process

from . import db, maestros
from .certificacion import Certificacion, M, clasificar_capitulo
from .config import TIPOS_COMPUTABLES
from .pdf_utils import sha256, store_pdf
from .money import fmt_eur
import math

SCHEMA = """
CREATE TABLE IF NOT EXISTS certificaciones (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    numero INTEGER NOT NULL, fecha TEXT, presupuesto_codigo TEXT, cliente TEXT, obra_texto TEXT,
    file_hash TEXT, file_path TEXT, filename TEXT, paginas INTEGER,
    total_origen_m INTEGER, total_anterior_m INTEGER, total_actual_m INTEGER,
    verificada INTEGER DEFAULT 0, avisos TEXT,
    importado_por TEXT, importado_en TEXT,
    UNIQUE (obra_id, numero)
);
CREATE TABLE IF NOT EXISTS cert_capitulos (
    id INTEGER PRIMARY KEY,
    cert_id INTEGER NOT NULL REFERENCES certificaciones(id) ON DELETE CASCADE,
    codigo TEXT NOT NULL, nombre TEXT, padre TEXT, nivel INTEGER, orden INTEGER, tipo TEXT,
    origen_m INTEGER, anterior_m INTEGER, actual_m INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cc_cert ON cert_capitulos(cert_id);
CREATE TABLE IF NOT EXISTS cert_lineas (
    id INTEGER PRIMARY KEY,
    cert_id INTEGER NOT NULL REFERENCES certificaciones(id) ON DELETE CASCADE,
    orden INTEGER, capitulo TEXT, subcapitulo TEXT, codigo TEXT, unidad TEXT, titulo TEXT, descripcion TEXT,
    pct_origen TEXT, cant_origen TEXT, precio TEXT, cant_presupuesto TEXT,
    origen_m INTEGER, anterior_m INTEGER, actual_m INTEGER, presupuesto_m INTEGER,
    cant_anterior TEXT, cant_actual TEXT, tipo TEXT
);
CREATE INDEX IF NOT EXISTS ix_cl_cert ON cert_lineas(cert_id);
CREATE INDEX IF NOT EXISTS ix_cl_cod ON cert_lineas(codigo);

CREATE TABLE IF NOT EXISTS ofertas (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    partida_id INTEGER REFERENCES partidas(id) ON DELETE SET NULL,
    cert_partida TEXT,
    proveedor_id INTEGER REFERENCES proveedores(id),
    proveedor_nombre TEXT,
    alcance TEXT,
    importe_cents INTEGER NOT NULL DEFAULT 0,
    fecha TEXT,
    estado TEXT DEFAULT 'recibida',
    valorado_por TEXT,
    responsable TEXT,
    puntuacion INTEGER,
    opinion TEXT,
    condiciones TEXT,
    plazo TEXT,
    creado_por TEXT, creado_en TEXT
);
CREATE INDEX IF NOT EXISTS ix_of_obra ON ofertas(obra_id);

CREATE TABLE IF NOT EXISTS costes_manuales (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    partida_id INTEGER REFERENCES partidas(id) ON DELETE SET NULL,
    concepto TEXT, importe_cents INTEGER NOT NULL, fecha TEXT, fuente TEXT,
    creado_por TEXT, creado_en TEXT
);
"""

ESTADOS_OFERTA = ["solicitada", "recibida", "en_negociacion", "adjudicada", "descartada"]
MODOS_CLASIF = {"ia": "IA", "reglas": "Reglas", "manual": "Manual", "cert": "Similitud con certificación"}

STOP = set("""de del la las el los y en con para por sin a al o u e un una uno incluso incluido incluida totalmente
terminado segun según mano obra suministro colocacion colocación instalacion instalación medios auxiliares pp p.p
material materiales tipo ud m2 m3 ml cm mm formado formada realizada realizado mediante hasta desde sobre entre
como otros todo toda todos todas cada precio segun proyecto documentacion grafica""".split())


def init(con):
    con.executescript(SCHEMA)
    for col, ddl in (("responsable", "TEXT"), ("notas", "TEXT"), ("tipo", "TEXT"), ("origen_estructura", "TEXT")):
        try:
            con.execute(f"ALTER TABLE partidas ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    for col, ddl in (("cert_partida", "TEXT"), ("cert_confianza", "REAL")):
        try:
            con.execute(f"ALTER TABLE lineas ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    con.commit()


def norm_t(t) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(t or "").upper())[:30]


def m2d(m) -> Decimal:
    return Decimal(int(m or 0)) / 1000


def c2m(c) -> int:
    return int(c or 0) * 10


# ============================================================================ certificaciones
def buscar_obra_para_cert(con, cert: Certificacion) -> int | None:
    """Presupuesto '240664' -> obra '664'; si no, por nombre/alias."""
    for o in maestros.listar_obras(con, solo_activas=False):
        if cert.presupuesto and cert.presupuesto.endswith(o["codigo"]):
            return o["id"]
    oid, conf, _ = maestros.identificar_obra(con, cert.obra or "", cert.presupuesto or "")
    return oid if conf >= 0.8 else None


def guardar_certificacion(con, cert: Certificacion, data: bytes | None, filename: str, obra_id: int,
                          usuario: str, reemplazar: bool = False) -> int:
    ex = db.one(con, "SELECT id FROM certificaciones WHERE obra_id=? AND numero=?", (obra_id, cert.numero))
    if ex and not reemplazar:
        raise ValueError(f"La certificación nº {cert.numero} ya está importada en esta obra.")
    from .certificacion import verificar
    if getattr(cert, "_sin_anterior", False):
        # hoja solo con importes a origen: el «anterior» es el origen de la certificación previa (misma partida)
        prev = db.one(con, "SELECT id FROM certificaciones WHERE obra_id=? AND numero<? ORDER BY numero DESC LIMIT 1",
                      (obra_id, cert.numero or 10 ** 6))
        prev_l = db.rows(con, "SELECT * FROM cert_lineas WHERE cert_id=? ORDER BY orden", (prev["id"],)) if prev else []
        por_cod, por_raiz = defaultdict(list), defaultdict(list)
        for r in prev_l:
            por_cod[r["codigo"]].append(r)
            por_raiz[re.split(r"[\s(]", r["codigo"])[0]].append(r)
        mismo_precio = lambda x, l_: abs(Decimal(x["precio"]) - l_.precio) < Decimal("0.001")  # noqa: E731
        mismo_titulo = lambda x, l_: norm_t(x["titulo"])[:15] == norm_t(l_.titulo)[:15]  # noqa: E731
        for l in cert.lineas:
            cod_raiz = re.split(r"[\s(]", l.codigo)[0]
            c1 = [r for r in por_cod.get(l.codigo, []) if not r.get("_usada")]
            c2 = [r for r in por_raiz.get(cod_raiz, []) if not r.get("_usada")]
            c3 = [r for r in prev_l if not r.get("_usada")]
            # emparejado por pasos: mismo código (+precio, +título) -> código raíz («DSFV ER», «14.14(1)») -> precio y título
            r = next((x for x in c1 if mismo_precio(x, l) and mismo_titulo(x, l)), None) or \
                next((x for x in c1 if mismo_precio(x, l)), None) or \
                next((x for x in c2 if mismo_precio(x, l)), None) or \
                (c1[0] if c1 else None) or \
                next((x for x in c3 if mismo_precio(x, l) and mismo_titulo(x, l) and l.precio != 0), None)
            if r:
                r["_usada"] = True
            l.imp_anterior = (Decimal(r["origen_m"]) / 1000) if r else Decimal(0)
            l.imp_actual = l.imp_origen - l.imp_anterior
            l.cant_anterior = Decimal(r["cant_origen"]) if r and r["cant_origen"] else Decimal(0)
            l.cant_actual = l.cant_origen - l.cant_anterior
        for k in cert.capitulos:
            ls = [l for l in cert.lineas if (l.capitulo == k.codigo if k.nivel == 1 else l.subcapitulo == k.codigo)]
            k.tot_anterior = sum((l.imp_anterior for l in ls), Decimal(0))
            k.tot_actual = sum((l.imp_actual for l in ls), Decimal(0))
        cert.totales = (cert.totales[0], sum((l.imp_anterior for l in cert.lineas), Decimal(0)),
                        sum((l.imp_actual for l in cert.lineas), Decimal(0)))
        cert.avisos = [a for a in cert.avisos if "solo trae el importe" not in a]
    avisos = verificar(cert)
    h, path = (store_pdf(data) if data else (None, None))
    tot = cert.totales or (0, 0, 0)
    with db.tx(con):
        if ex:
            con.execute("DELETE FROM certificaciones WHERE id=?", (ex["id"],))
        cur = con.execute(
            "INSERT INTO certificaciones (obra_id, numero, fecha, presupuesto_codigo, cliente, obra_texto, file_hash, file_path, "
            "filename, paginas, total_origen_m, total_anterior_m, total_actual_m, verificada, avisos, importado_por, importado_en) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obra_id, cert.numero, cert.fecha, cert.presupuesto, cert.cliente, cert.obra, h, str(path) if path else None,
             filename, cert.paginas, M(tot[0]), M(tot[1] if len(tot) > 1 else 0), M(tot[2] if len(tot) > 2 else 0),
             int(not avisos), "\n".join([f"{s_}: {m_}" for s_, m_ in avisos[:60]] +
                                         ([f"… y {len(avisos) - 60} avisos más"] if len(avisos) > 60 else [])), usuario, db.now_iso()))
        cid = cur.lastrowid
        tipos = {}
        for k in cert.capitulos:
            raiz = k.codigo if k.nivel == 1 else None
            tipos[k.codigo] = clasificar_capitulo(k.codigo, k.nombre)
            con.execute("INSERT INTO cert_capitulos (cert_id, codigo, nombre, padre, nivel, orden, tipo, origen_m, anterior_m, actual_m) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (cid, k.codigo, k.nombre, k.padre, k.nivel, k.orden, tipos[k.codigo] if raiz else None,
                         M(k.tot_origen or 0), M(k.tot_anterior or 0), M(k.tot_actual or 0)))
        for l in cert.lineas:
            con.execute(
                "INSERT INTO cert_lineas (cert_id, orden, capitulo, subcapitulo, codigo, unidad, titulo, descripcion, pct_origen, "
                "cant_origen, precio, cant_presupuesto, origen_m, anterior_m, actual_m, presupuesto_m, cant_anterior, cant_actual, tipo) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, l.orden, l.capitulo, l.subcapitulo, l.codigo, l.unidad, l.titulo, l.descripcion, str(l.pct_origen),
                 str(l.cant_origen), str(l.precio), str(l.cant_presupuesto) if l.cant_presupuesto is not None else None,
                 M(l.imp_origen), M(l.imp_anterior), M(l.imp_actual),
                 M(l.imp_presupuesto) if l.imp_presupuesto is not None else None,
                 str(l.cant_anterior), str(l.cant_actual), tipos.get(l.capitulo, "contrato")))
        db.audit(con, usuario, "importar_certificacion", "obra", obra_id,
                 {"numero": cert.numero, "fecha": cert.fecha, "origen": str(tot[0]), "avisos": len(avisos)})
    return cid


def certificaciones(con, obra_id: int) -> list[dict]:
    return db.rows(con, "SELECT * FROM certificaciones WHERE obra_id=? ORDER BY numero", (obra_id,))


def ultima_cert(con, obra_id: int) -> dict | None:
    return db.one(con, "SELECT * FROM certificaciones WHERE obra_id=? ORDER BY numero DESC LIMIT 1", (obra_id,))


def cert_lineas_df(con, cert_id: int) -> pd.DataFrame:
    return pd.DataFrame(db.rows(con, "SELECT * FROM cert_lineas WHERE cert_id=? ORDER BY orden", (cert_id,)))


def cert_capitulos_df(con, cert_id: int) -> pd.DataFrame:
    return pd.DataFrame(db.rows(con, "SELECT * FROM cert_capitulos WHERE cert_id=? ORDER BY orden", (cert_id,)))


def _con_clave(rows: list[dict]) -> dict[str, dict]:
    """Los códigos se repiten entre capítulos: clave = capítulo/subcapítulo/código#ocurrencia."""
    vistos, out = Counter(), {}
    for r in rows:
        base = f"{r['capitulo']}/{r['subcapitulo'] or ''}/{r['codigo']}"
        vistos[base] += 1
        out[f"{base}#{vistos[base]}"] = r
    return out


def comprobar_encadenado(con, obra_id: int) -> list[str]:
    """Cert N: 'anterior' de cada partida debe ser igual a 'origen' de la cert N-1."""
    cs = certificaciones(con, obra_id)
    out = []
    for a, b in zip(cs, cs[1:]):
        if b["numero"] != a["numero"] + 1:
            out.append(f"Faltan certificaciones entre la nº {a['numero']} y la nº {b['numero']}")
            continue
        if a["total_origen_m"] != b["total_anterior_m"]:
            out.append(f"Nº {b['numero']}: total anterior {m2d(b['total_anterior_m'])} ≠ origen de la nº {a['numero']} "
                       f"{m2d(a['total_origen_m'])} (diferencia {m2d(b['total_anterior_m'] - a['total_origen_m'])})")
        la = _con_clave(db.rows(con, "SELECT * FROM cert_lineas WHERE cert_id=? ORDER BY orden", (a["id"],)))
        lb = _con_clave(db.rows(con, "SELECT * FROM cert_lineas WHERE cert_id=? ORDER BY orden", (b["id"],)))
        for k, r in lb.items():
            if k in la and la[k]["origen_m"] != r["anterior_m"]:
                out.append(f"Nº {b['numero']} partida {r['codigo']} (cap. {r['capitulo']}): anterior {fmt_eur(m2d(r['anterior_m']))} "
                           f"≠ origen de la nº {a['numero']} {fmt_eur(m2d(la[k]['origen_m']))}")
    return out


def comparar(con, cert_a: int, cert_b: int) -> pd.DataFrame:
    """Cambios entre dos certificaciones: partidas nuevas, eliminadas, cambios de precio y de previsión."""
    A = _con_clave(db.rows(con, "SELECT * FROM cert_lineas WHERE cert_id=? ORDER BY orden", (cert_a,)))
    B = _con_clave(db.rows(con, "SELECT * FROM cert_lineas WHERE cert_id=? ORDER BY orden", (cert_b,)))
    filas = []
    for k in list(B) + [k for k in A if k not in B]:
        ra, rb = A.get(k), B.get(k)
        base = rb or ra
        f = {"codigo": base["codigo"], "capitulo": base["capitulo"], "titulo": base["titulo"]}
        if ra is None:
            filas.append({**f, "cambio": "Nueva partida", "antes": None, "despues": rb["precio"], "impacto_m": rb["origen_m"]})
            continue
        if rb is None:
            filas.append({**f, "cambio": "Partida eliminada", "antes": ra["precio"], "despues": None, "impacto_m": -ra["origen_m"]})
            continue
        if Decimal(ra["precio"]) != Decimal(rb["precio"]):
            filas.append({**f, "cambio": "Cambio de precio", "antes": ra["precio"], "despues": rb["precio"],
                          "impacto_m": M((Decimal(rb["precio"]) - Decimal(ra["precio"])) * Decimal(rb["cant_origen"]))})
        if ra["cant_presupuesto"] and rb["cant_presupuesto"]:
            qa, qb = Decimal(ra["cant_presupuesto"]), Decimal(rb["cant_presupuesto"])
            if qa and abs(qb - qa) / abs(qa) > Decimal("0.005"):   # tolerancia 0,5 %: el % a origen viene redondeado
                filas.append({**f, "cambio": "Cambio de medición prevista", "antes": ra["cant_presupuesto"],
                              "despues": rb["cant_presupuesto"], "impacto_m": (rb["presupuesto_m"] or 0) - (ra["presupuesto_m"] or 0)})
        if rb["origen_m"] < ra["origen_m"]:
            filas.append({**f, "cambio": "Retroceso a origen (descertificado)", "antes": str(m2d(ra["origen_m"])),
                          "despues": str(m2d(rb["origen_m"])), "impacto_m": rb["origen_m"] - ra["origen_m"]})
    return pd.DataFrame(filas)


# ============================================================================ estructura de coste
def palabras_capitulo(con, cert_id: int, capitulo: str, n: int = 25) -> str:
    tit = [r["titulo"] or "" for r in db.rows(con, "SELECT titulo FROM cert_lineas WHERE cert_id=? AND capitulo=?", (cert_id, capitulo))]
    cnt = Counter()
    for t in tit:
        for w in re.findall(r"[a-záéíóúñü]{4,}", maestros.strip_accents(t)):
            if w not in STOP:
                cnt[w] += 1
    return ", ".join(w for w, _ in cnt.most_common(n))


def adoptar_estructura(con, obra_id: int, cert_id: int, usuario: str) -> dict:
    """
    Sustituye las partidas genéricas de la obra por los CAPÍTULOS de la certificación
    (la misma estructura con la que se vende). Reclasifica las líneas de factura:
    - manuales: por similitud del nombre de la partida antigua con el capítulo;
    - resto: por palabras clave (las del capítulo + las de la partida genérica equivalente).
    """
    from . import historial
    historial.init(con)
    historial.foto_estructura(con, obra_id, usuario, "antes de usar los capítulos de la certificación")
    con.commit()
    caps = db.rows(con, "SELECT * FROM cert_capitulos WHERE cert_id=? AND nivel=1 ORDER BY orden", (cert_id,))
    antiguas = maestros.partidas_de_obra(con, obra_id)
    with db.tx(con):
        con.execute("UPDATE partidas SET codigo='OLD-'||codigo WHERE obra_id=? AND codigo NOT LIKE 'OLD-%'", (obra_id,))
        nuevas = {}
        for k in caps:
            kw = palabras_capitulo(con, cert_id, k["codigo"])
            # hereda palabras de la partida genérica más parecida (p.ej. 'Solados y alicatados' -> 'SOLADOS, ALICATADOS…')
            similar = process.extractOne(maestros.strip_accents(k["nombre"]),
                                         {p["id"]: maestros.strip_accents(p["descripcion"]) for p in antiguas},
                                         scorer=fuzz.token_set_ratio)
            if similar and similar[1] >= 60:
                pg = next(p for p in antiguas if p["id"] == similar[2])
                kw = ", ".join(x for x in [pg["palabras_clave"], kw] if x)
            venta = db.one(con, "SELECT COALESCE(SUM(COALESCE(presupuesto_m, origen_m)),0) s FROM cert_lineas WHERE cert_id=? AND capitulo=?",
                           (cert_id, k["codigo"]))["s"]
            cur = con.execute("INSERT INTO partidas (obra_id, codigo, descripcion, palabras_clave, presupuesto_venta_cents, tipo, origen_estructura) "
                              "VALUES (?,?,?,?,?,?,?)", (obra_id, k["codigo"], k["nombre"].title(), kw, round(venta / 10), k["tipo"],
                                                         f"cert:{cert_id}"))
            nuevas[k["codigo"]] = cur.lastrowid
        cur = con.execute("INSERT INTO partidas (obra_id, codigo, descripcion, tipo, origen_estructura) VALUES (?,?,?,?,?)",
                          (obra_id, "99", "Sin asignar", "sin_asignar", f"cert:{cert_id}"))
        p99 = cur.lastrowid
        nuevas_rows = maestros.partidas_de_obra(con, obra_id)
        nuevas_rows = [p for p in nuevas_rows if not p["codigo"].startswith("OLD-")]
        mapa_old = {}
        for p in antiguas:
            if p["codigo"] == "99":
                mapa_old[p["id"]] = p99
                continue
            best = process.extractOne(maestros.strip_accents(p["descripcion"]),
                                      {q["id"]: maestros.strip_accents(q["descripcion"]) for q in nuevas_rows if q["codigo"] != "99"},
                                      scorer=fuzz.token_set_ratio)
            mapa_old[p["id"]] = best[2] if best and best[1] >= 60 else None
        lin = db.rows(con, "SELECT l.id, l.descripcion, l.partida_id, l.partida_origen FROM lineas l JOIN documentos d ON d.id=l.documento_id "
                           "WHERE d.obra_id=?", (obra_id,))
        stats = Counter()
        for l in lin:
            pid, origen, conf = None, "reglas", None
            if l["partida_origen"] == "manual" and mapa_old.get(l["partida_id"]):
                pid, origen, conf = mapa_old[l["partida_id"]], "manual", 1.0
            else:
                pid, conf = maestros.clasificar_linea(l["descripcion"] or "", nuevas_rows)
                if not pid and mapa_old.get(l["partida_id"]):
                    pid, conf = mapa_old[l["partida_id"]], 0.5
            con.execute("UPDATE lineas SET partida_id=?, partida_origen=?, partida_confianza=? WHERE id=?",
                        (pid or p99, origen, conf, l["id"]))
            stats["asignadas" if pid else "sin_asignar"] += 1
        con.execute("UPDATE ofertas SET partida_id=NULL WHERE obra_id=? AND partida_id IN (SELECT id FROM partidas WHERE obra_id=? AND codigo LIKE 'OLD-%')",
                    (obra_id, obra_id))
        con.execute("UPDATE costes_manuales SET partida_id=NULL WHERE obra_id=? AND partida_id IN (SELECT id FROM partidas WHERE obra_id=? AND codigo LIKE 'OLD-%')",
                    (obra_id, obra_id))
        con.execute("DELETE FROM partidas WHERE obra_id=? AND codigo LIKE 'OLD-%'", (obra_id,))
        db.audit(con, usuario, "adoptar_estructura_certificacion", "obra", obra_id, {"cert_id": cert_id, **stats})
    from .validation import validar_y_guardar
    for d in db.rows(con, "SELECT id FROM documentos WHERE obra_id=? AND estado<>'sin_procesar'", (obra_id,)):
        validar_y_guardar(con, d["id"])
    return dict(stats)


def _tokens(t: str) -> set[str]:
    """Raíces de 6 letras de las palabras significativas (solado/solados, pintura/pintado…)."""
    return {w[:6] for w in re.findall(r"[a-z0-9ñ]{3,}", maestros.strip_accents(t or "")) if w not in STOP}


_IDX_CACHE: dict = {}


def _indice_cert(con, cert_id: int):
    if cert_id in _IDX_CACHE:
        return _IDX_CACHE[cert_id]
    ls = db.rows(con, "SELECT codigo, titulo, capitulo, unidad, precio, descripcion FROM cert_lineas WHERE cert_id=? "
                      "AND CAST(precio AS REAL)<>0", (cert_id,))
    toks = [_tokens(f"{l['titulo']} {l['titulo']} {(l['descripcion'] or '')[:200]}") for l in ls]
    df = Counter(t for ts in toks for t in ts)
    n = max(1, len(ls))
    idf = {t: math.log(1 + n / c) for t, c in df.items()}
    _IDX_CACHE[cert_id] = (ls, toks, idf)
    return _IDX_CACHE[cert_id]


def sugerir_partida_cert(con, obra_id: int, texto: str, n: int = 3) -> list[dict]:
    """
    Partidas de la última certificación más parecidas a un concepto de factura.
    Puntuación = Σ idf(palabras comunes) / Σ idf(palabras del concepto): premia coincidir en palabras
    raras y específicas (LAKESTONE, electrocerradura) y no en genéricas (instalación, suministro).
    """
    c = ultima_cert(con, obra_id)
    if not c or not texto:
        return []
    ls, toks, idf = _indice_cert(con, c["id"])
    q = _tokens(texto)
    conocidas = [t for t in q if t in idf]
    den = sum(idf[t] for t in conocidas)
    if not den:
        return []
    cobertura_q = math.sqrt(len(conocidas) / len(q))   # penaliza conceptos con muchas palabras ajenas a la obra
    res = []
    for l, ts in zip(ls, toks):
        comun = q & ts
        if comun:
            sc = sum(idf[t] for t in comun) / den * cobertura_q
            res.append({**l, "score": round(sc, 2), "comunes": ", ".join(sorted(comun))})
    return sorted(res, key=lambda r: -r["score"])[:n]


def relacionar_automatico(con, obra_id: int, umbral: float = 0.6, usuario: str = "") -> int:
    """Asigna partida de certificación a las líneas de factura sin relacionar con similitud ≥ umbral."""
    lin = db.rows(con, "SELECT l.id, l.descripcion FROM lineas l JOIN documentos d ON d.id=l.documento_id "
                       "WHERE d.obra_id=? AND (l.cert_partida IS NULL OR l.cert_partida='')", (obra_id,))
    n = 0
    with db.tx(con):
        for l in lin:
            s = sugerir_partida_cert(con, obra_id, l["descripcion"] or "", 1)
            if s and s[0]["score"] >= umbral:
                con.execute("UPDATE lineas SET cert_partida=?, cert_confianza=? WHERE id=?", (s[0]["codigo"], s[0]["score"], l["id"]))
                n += 1
        db.audit(con, usuario, "relacionar_partidas_cert", "obra", obra_id, {"lineas": n, "umbral": umbral})
    return n


# ============================================================================ ofertas / contratos
def ofertas_df(con, obra_id: int) -> pd.DataFrame:
    return pd.DataFrame(db.rows(con, """
        SELECT o.*, p.codigo AS partida_codigo, p.descripcion AS partida,
               COALESCE(pr.nombre, o.proveedor_nombre) AS proveedor
        FROM ofertas o LEFT JOIN partidas p ON p.id=o.partida_id LEFT JOIN proveedores pr ON pr.id=o.proveedor_id
        WHERE o.obra_id=? ORDER BY p.codigo, o.importe_cents""", (obra_id,)))


def guardar_oferta(con, datos: dict, usuario: str, oferta_id: int | None = None) -> int:
    campos = ["obra_id", "partida_id", "cert_partida", "proveedor_id", "proveedor_nombre", "alcance", "importe_cents", "fecha",
              "estado", "valorado_por", "responsable", "puntuacion", "opinion", "condiciones", "plazo"]
    vals = [datos.get(c) for c in campos]
    with db.tx(con):
        if oferta_id:
            con.execute(f"UPDATE ofertas SET {', '.join(c + '=?' for c in campos)} WHERE id=?", vals + [oferta_id])
        else:
            cur = con.execute(f"INSERT INTO ofertas ({', '.join(campos)}, creado_por, creado_en) VALUES ({','.join('?' * (len(campos) + 2))})",
                              vals + [usuario, db.now_iso()])
            oferta_id = cur.lastrowid
        db.audit(con, usuario, "oferta", "oferta", oferta_id, {k: datos.get(k) for k in ("estado", "importe_cents", "proveedor_nombre")})
    return oferta_id


def comparativa_partida(con, obra_id: int, partida_id: int) -> dict:
    df = ofertas_df(con, obra_id)
    df = df[(df["partida_id"] == partida_id) & (df["estado"] != "descartada")] if not df.empty else df
    if df.empty:
        return {"n": 0}
    imp = df["importe_cents"].astype("int64")
    mn, mx = int(imp.min()), int(imp.max())
    adj = df[df["estado"] == "adjudicada"]
    return {"n": len(df), "min_c": mn, "max_c": mx, "media_c": int(round(imp.mean())),
            "dispersion_pct": round((mx - mn) / mn * 100, 1) if mn else None,
            "adjudicado_c": int(adj["importe_cents"].sum()) if not adj.empty else 0,
            "sobre_minimo_c": int(adj["importe_cents"].sum()) - mn if not adj.empty else None}


# ============================================================================ rentabilidad
def _coste_por_partida(con, obra_id: int, desde=None, hasta=None, estados=None) -> dict[int, int]:
    from .analytics import lineas_coste_df
    lin = lineas_coste_df(con, obra_id=obra_id, desde=desde, hasta=hasta, estados=estados)
    out = Counter()
    if not lin.empty:
        for pid, v in lin.groupby(lin["partida_id"].fillna(-1))["importe_c"].sum().items():
            out[int(pid)] += int(v)
    q = "SELECT partida_id, SUM(importe_cents) s FROM costes_manuales WHERE obra_id=?"
    p = [obra_id]
    if desde:
        q += " AND fecha>=?"; p.append(str(desde))
    if hasta:
        q += " AND fecha<=?"; p.append(str(hasta))
    manual = {int(r["partida_id"] or -1): int(r["s"]) for r in db.rows(con, q + " GROUP BY partida_id", p)}
    # costes internos (mano de obra propia, indirectos, gastos generales…) y cargos a subcontratas (restan)
    try:
        from . import internos
        internos.init(con)
        ri = internos.resumen(con, obra_id, str(desde) if desde else None, str(hasta) if hasta else None)
        for pid, v in ri["por_partida_coste"].items():
            manual[int(pid)] = manual.get(int(pid), 0) + int(round(v * 100))
    except Exception:
        pass
    return dict(out), manual


def periodo_cert(con, cert: dict) -> tuple[str | None, str | None]:
    """Ventana de fechas del mes certificado: día siguiente a la cert anterior → fecha de esta cert."""
    prev = db.one(con, "SELECT fecha FROM certificaciones WHERE obra_id=? AND numero<? ORDER BY numero DESC LIMIT 1",
                  (cert["obra_id"], cert["numero"]))
    hasta = cert["fecha"]
    if prev and prev["fecha"]:
        desde = (datetime.strptime(prev["fecha"], "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    else:
        f = datetime.strptime(hasta, "%Y-%m-%d").date()
        desde = f.replace(day=1).isoformat()
    return desde, hasta


def rentabilidad(con, obra_id: int, cert_id: int, modo: str = "origen", estados=None, desde_mes=None, hasta_mes=None) -> pd.DataFrame:
    """
    Tabla por capítulo (partida de coste):
      venta_prevista_m, certificado_m (origen o del mes), avance_pct,
      coste_facturas_m, coste_manual_m, coste_m, contratado_m, margen_m, margen_pct,
      margen_previsto_m (venta prevista − contratado), pendiente_contrato_m (contratado − coste)
    modo: 'origen' = certificado a origen vs coste acumulado hasta la fecha de la certificación
          'mes'    = certificado del mes vs coste del periodo de la certificación
    """
    cert = db.one(con, "SELECT * FROM certificaciones WHERE id=?", (cert_id,))
    caps = db.rows(con, "SELECT * FROM cert_capitulos WHERE cert_id=? AND nivel=1 ORDER BY orden", (cert_id,))
    venta_prev = {r["capitulo"]: int(r["s"] or 0) for r in db.rows(
        con, "SELECT capitulo, SUM(COALESCE(presupuesto_m, origen_m)) s FROM cert_lineas WHERE cert_id=? GROUP BY capitulo", (cert_id,))}
    partidas = {p["codigo"]: p for p in maestros.partidas_de_obra(con, obra_id)}
    if modo == "mes":
        desde, hasta = (desde_mes, hasta_mes) if desde_mes else periodo_cert(con, cert)
    else:
        desde, hasta = None, cert["fecha"]
    coste_f, coste_man = _coste_por_partida(con, obra_id, desde, hasta, estados)
    of = ofertas_df(con, obra_id)
    contratado = Counter()
    if not of.empty:
        for pid, v in of[of["estado"] == "adjudicada"].groupby("partida_id")["importe_cents"].sum().items():
            contratado[int(pid)] += int(v)
    filas = []
    usados = set()
    for k in caps:
        p = partidas.get(k["codigo"])
        pid = p["id"] if p else None
        usados.add(pid)
        cert_m = k["origen_m"] if modo == "origen" else k["actual_m"]
        cf, cm = c2m(coste_f.get(pid, 0)), c2m(coste_man.get(pid, 0))
        filas.append({"codigo": k["codigo"], "capitulo": k["nombre"], "tipo": k["tipo"], "partida_id": pid,
                      "responsable": p.get("responsable") if p else None,
                      "venta_prevista_m": venta_prev.get(k["codigo"], 0), "certificado_m": cert_m,
                      "certificado_origen_m": k["origen_m"], "coste_facturas_m": cf, "coste_manual_m": cm,
                      "contratado_m": c2m(contratado.get(pid, 0))})
    # costes en partidas que no son capítulos de la certificación (99, genéricas…)
    for cod, p in partidas.items():
        if p["id"] in usados:
            continue
        cf, cm = c2m(coste_f.get(p["id"], 0)), c2m(coste_man.get(p["id"], 0))
        if cf or cm or contratado.get(p["id"]):
            filas.append({"codigo": cod, "capitulo": p["descripcion"], "tipo": "sin_venta", "partida_id": p["id"],
                          "responsable": p.get("responsable"), "venta_prevista_m": 0, "certificado_m": 0,
                          "certificado_origen_m": 0, "coste_facturas_m": cf, "coste_manual_m": cm,
                          "contratado_m": c2m(contratado.get(p["id"], 0))})
    if coste_f.get(-1) or coste_man.get(-1):
        filas.append({"codigo": "—", "capitulo": "Sin partida", "tipo": "sin_venta", "partida_id": None, "responsable": None,
                      "venta_prevista_m": 0, "certificado_m": 0, "certificado_origen_m": 0,
                      "coste_facturas_m": c2m(coste_f.get(-1, 0)), "coste_manual_m": c2m(coste_man.get(-1, 0)), "contratado_m": 0})
    df = pd.DataFrame(filas)
    if df.empty:
        return df
    df["coste_m"] = df["coste_facturas_m"] + df["coste_manual_m"]
    # ventas internas (obra ejecutada sin certificar, revisión de precios, extras aprobados…): cuentan en el margen interno
    df["venta_interna_m"] = 0
    try:
        from . import internos
        ri = internos.resumen(con, obra_id, desde, hasta)
        for pid, v in ri["por_partida_venta"].items():
            m_ = int(round(v * 1000))
            if pid != -1 and (df["partida_id"] == pid).any():
                df.loc[df["partida_id"] == pid, "venta_interna_m"] += m_
            else:
                fila = {c: (0 if str(df[c].dtype).startswith(("int", "float")) else None) for c in df.columns}
                fila.update({"codigo": "—", "capitulo": "VENTAS INTERNAS SIN CAPÍTULO", "venta_interna_m": m_})
                df = pd.concat([df, pd.DataFrame([fila])], ignore_index=True)
    except Exception:
        pass
    df["margen_m"] = df["certificado_m"] + df["venta_interna_m"] - df["coste_m"]
    df["margen_pct"] = df.apply(lambda r: round(r["margen_m"] / r["certificado_m"] * 100, 1) if r["certificado_m"] else None, axis=1)
    df["avance_pct"] = df.apply(lambda r: round(r["certificado_origen_m"] / r["venta_prevista_m"] * 100, 1)
                                if r["venta_prevista_m"] else None, axis=1)
    df["margen_previsto_m"] = df.apply(lambda r: r["venta_prevista_m"] - r["contratado_m"] if r["contratado_m"] else None, axis=1)
    # presupuesto de coste de Estudios (importado en Obras y partidas): coste esperado según el avance y desviación
    ppto = {p_["id"]: c2m(p_.get("presupuesto_coste_cents") or 0) for p_ in partidas.values()}
    df["ppto_estudio_m"] = df["partida_id"].map(lambda i: ppto.get(i, 0) if i == i and i is not None else 0).fillna(0).astype("int64")
    df["coste_esperado_m"] = df.apply(lambda r: int(r["ppto_estudio_m"] * (r["avance_pct"] or 0) / 100) if r["ppto_estudio_m"] and r["avance_pct"] else None, axis=1)
    df["desv_estudio_m"] = df.apply(lambda r: r["coste_m"] - r["coste_esperado_m"] if r["coste_esperado_m"] is not None and modo == "origen" else None, axis=1)
    df["pendiente_contrato_m"] = df.apply(lambda r: r["contratado_m"] - r["coste_m"] if r["contratado_m"] else None, axis=1)
    df.attrs["periodo"] = (desde, hasta)
    return df


def alertas_rentabilidad(df: pd.DataFrame, cobertura_coste: float) -> list[tuple[str, str]]:
    """Reglas simples y explicables sobre la tabla de rentabilidad."""
    out = []
    if df.empty:
        return out
    if cobertura_coste < 0.5:
        out.append(("info", "El coste cargado cubre poco periodo respecto a lo certificado: los márgenes a origen están "
                            "sobreestimados. Usa el modo «mes de la certificación» o importa el coste a origen (manual/SIS)."))
    for r in df.itertuples():
        if r.certificado_m > 0 and r.coste_m > r.certificado_m:
            out.append(("alta", f"{r.codigo} {r.capitulo}: el coste ({fmt_eur(m2d(r.coste_m))}) supera lo certificado "
                                f"({fmt_eur(m2d(r.certificado_m))})"))
        if r.contratado_m and r.coste_m > r.contratado_m:
            out.append(("alta", f"{r.codigo} {r.capitulo}: facturado por encima de lo contratado "
                                f"({fmt_eur(m2d(r.coste_m - r.contratado_m))} de exceso)"))
        if r.certificado_m == 0 and r.coste_m > 0 and r.tipo != "sin_venta":
            out.append(("media", f"{r.codigo} {r.capitulo}: hay coste pero nada certificado en el periodo "
                                 "(¿obra ejecutada sin certificar o factura mal imputada?)"))
        if r.tipo == "sin_venta" and r.coste_m > 0:
            out.append(("media", f"{r.codigo} {r.capitulo}: coste sin capítulo de venta asociado ({fmt_eur(m2d(r.coste_m))})"))
        if r.margen_previsto_m is not None and r.margen_previsto_m < 0:
            out.append(("alta", f"{r.codigo} {r.capitulo}: lo contratado supera la venta prevista del capítulo"))
    return out


def serie_mensual(con, obra_id: int) -> pd.DataFrame:
    """Venta certificada por certificación vs coste facturado en su periodo."""
    from .analytics import documentos_df
    filas = []
    for c in certificaciones(con, obra_id):
        desde, hasta = periodo_cert(con, c)
        d = documentos_df(con, obra_id=obra_id, desde=desde, hasta=hasta)
        coste = c2m(int(d["base_c"].sum()) if not d.empty else 0)
        filas.append({"cert": c["numero"], "fecha": c["fecha"], "desde": desde, "hasta": hasta,
                      "venta_mes_m": c["total_actual_m"], "venta_origen_m": c["total_origen_m"], "coste_mes_m": coste,
                      "margen_mes_m": c["total_actual_m"] - coste})
    return pd.DataFrame(filas)


def cobertura(con, obra_id: int, cert: dict) -> float:
    """Fracción aproximada del plazo certificado cubierta por facturas cargadas (para avisar de márgenes engañosos)."""
    r = db.one(con, "SELECT MIN(fecha) mi, MAX(fecha) ma FROM documentos WHERE obra_id=? AND estado<>'rechazada' AND fecha IS NOT NULL",
               (obra_id,))
    man = db.one(con, "SELECT COUNT(*) n FROM costes_manuales WHERE obra_id=?", (obra_id,))["n"]
    if man:
        return 1.0
    if not r or not r["mi"] or not cert.get("numero"):
        return 0.0
    dias = (datetime.strptime(r["ma"], "%Y-%m-%d") - datetime.strptime(r["mi"], "%Y-%m-%d")).days + 30
    return min(1.0, dias / (30.4 * cert["numero"]))


def precios_venta_vs_coste(con, obra_id: int) -> pd.DataFrame:
    """Líneas de factura relacionadas con una partida de certificación: precio unitario de coste vs de venta."""
    c = ultima_cert(con, obra_id)
    if not c:
        return pd.DataFrame()
    return pd.DataFrame(db.rows(con, """
        SELECT l.documento_id, d.numero, COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, d.fecha,
               l.descripcion AS concepto_factura, l.cantidad, l.unidad, l.precio_unitario AS precio_coste, l.importe_cents,
               cl.codigo AS partida_cert, cl.titulo AS partida_venta, cl.unidad AS unidad_venta, cl.precio AS precio_venta,
               l.cert_confianza
        FROM lineas l JOIN documentos d ON d.id=l.documento_id
        LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
        JOIN cert_lineas cl ON cl.cert_id=? AND cl.codigo=l.cert_partida
        WHERE d.obra_id=? AND d.estado<>'rechazada'
        ORDER BY cl.codigo""", (c["id"], obra_id)))


# ============================================================================ coste devengado pendiente de factura
def devengado_pendiente(con, obra_id: int | None = None, hasta: str | None = None) -> list[dict]:
    """
    Obra ejecutada o material recibido que aún no se ha facturado: certificaciones de proveedor y albaranes sueltos
    sin factura que los recoja. Es coste real del periodo (provisión de cierre) aunque todavía no haya factura.
    Emparejado: misma empresa y (a) una factura con la misma base (±1 %) y fecha posterior, o (b) el nº de albarán
    aparece en las líneas de alguna factura.
    """
    q = """SELECT d.id, d.tipo_documento, d.emisor_nif, COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, d.numero, d.fecha,
                  d.base_imponible_cents AS base_c, d.obra_id, d.texto, d.albaranes
           FROM documentos d LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
           WHERE d.tipo_documento IN ('certificacion','albaran') AND d.estado NOT IN ('rechazada','eliminado','duplicado')
             AND COALESCE(d.base_imponible_cents,0) > 0"""
    p = []
    if obra_id:
        q += " AND d.obra_id=?"; p.append(obra_id)
    if hasta:
        q += " AND (d.fecha IS NULL OR d.fecha<=?)"; p.append(hasta)
    out = []
    for d in db.rows(con, q, p):
        if re.search(r"%OR\b", d["texto"] or ""):
            continue                                      # certificación a cliente subida por error: no es coste
        fac = db.one(con, """SELECT id, numero FROM documentos WHERE emisor_nif=? AND tipo_documento IN ('factura','anticipo')
                             AND estado NOT IN ('rechazada','eliminado','duplicado') AND ABS(COALESCE(base_imponible_cents,0)-?) <= ?
                             AND (fecha IS NULL OR ? IS NULL OR fecha >= date(?, '-5 day'))""",
                      (d["emisor_nif"], d["base_c"], max(100, abs(d["base_c"]) // 100), d["fecha"], d["fecha"])) if d["emisor_nif"] else None
        if not fac and d["tipo_documento"] == "albaran" and d["numero"]:
            n_alb = re.sub(r"[^A-Z0-9]", "", str(d["numero"]).upper())
            for r in db.rows(con, """SELECT DISTINCT l.albaran, dd.id, dd.numero FROM lineas l JOIN documentos dd ON dd.id=l.documento_id
                                     WHERE dd.emisor_nif=? AND l.albaran IS NOT NULL AND dd.estado NOT IN ('rechazada','eliminado')""",
                             (d["emisor_nif"],)):
                if n_alb and n_alb in re.sub(r"[^A-Z0-9]", "", r["albaran"].upper()):
                    fac = r
                    break
        if not fac:
            out.append({"id": d["id"], "tipo": d["tipo_documento"], "proveedor": d["proveedor"], "nif": d["emisor_nif"],
                        "numero": d["numero"], "fecha": d["fecha"], "base_c": d["base_c"], "obra_id": d["obra_id"]})
    try:                     # certificaciones propias al subcontratista aprobadas y aún sin factura
        from . import cert_proveedor
        cert_proveedor.casar_facturas(con, obra_id)
        for c in cert_proveedor.aprobadas_sin_factura(con, obra_id):
            if hasta and (c["fecha"] or c["periodo"] + "-01") > hasta:
                continue
            out.append({"id": None, "tipo": "cert_propia", "proveedor": c["proveedor"], "nif": None, "numero": f"CP {c['numero']} ({c['periodo']})",
                        "fecha": c["fecha"], "base_c": c["base_mes_cents"], "obra_id": c["obra_id"]})
    except Exception:  # noqa: BLE001
        pass
    return out
