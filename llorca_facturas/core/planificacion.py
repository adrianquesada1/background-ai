"""
PLANIFICACIÓN DE OBRA: planning inicial y replanificación cuando hay retrasos.

- Actividades con duración en días laborables, responsable (subcontrata o equipo propio), capítulo y dependencias
  (FC fin-comienzo, CC comienzo-comienzo, FF fin-fin, con desfase en días, p. ej. «3FC+2»).
- Calendario laboral: lunes a viernes, con festivos y periodos de cierre configurables por obra (agosto, Navidad…).
- Cálculo por el método del camino crítico (CPM): inicio y fin más tempranos y más tardíos, holgura y actividades
  críticas. Detecta dependencias circulares y referencias a actividades inexistentes.
- LÍNEA BASE: se congela el planning inicial aprobado para compararlo después.
- REPLANIFICACIÓN: con la fecha de control y lo real (inicio y fin reales, % de avance, días que faltan), se recalcula
  todo lo que depende de lo retrasado. Se enseña qué actividades se mueven, cuántos días, qué pasa a ser crítico y el
  retraso del fin de obra frente a la línea base.
- Importación desde Excel/CSV (id, actividad, duración, predecesoras…) y diagrama de Gantt en pantalla.
"""
from __future__ import annotations

import io
import json
import re
from collections import defaultdict, deque
from datetime import date, timedelta

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_actividades (
    id INTEGER PRIMARY KEY, obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    codigo TEXT NOT NULL, nombre TEXT NOT NULL, duracion INTEGER NOT NULL DEFAULT 1, predecesoras TEXT DEFAULT '',
    responsable TEXT, capitulo TEXT, oferta_id INTEGER, inicio_min TEXT,
    inicio_real TEXT, fin_real TEXT, avance INTEGER DEFAULT 0, restante INTEGER, notas TEXT, orden INTEGER,
    UNIQUE (obra_id, codigo));
CREATE TABLE IF NOT EXISTS plan_config (
    obra_id INTEGER PRIMARY KEY REFERENCES obras(id) ON DELETE CASCADE, inicio_obra TEXT, festivos TEXT DEFAULT '',
    sabados INTEGER DEFAULT 0, fecha_control TEXT);
CREATE TABLE IF NOT EXISTS plan_lineas_base (
    id INTEGER PRIMARY KEY, obra_id INTEGER NOT NULL, nombre TEXT, creada_por TEXT, creada_en TEXT, datos TEXT);
"""
TIPOS_DEP = {"FC": "Fin → comienzo", "CC": "Comienzo → comienzo", "FF": "Fin → fin"}


def init(con):
    con.executescript(SCHEMA)
    con.commit()


# ============================================================================ calendario laboral
class Calendario:
    def __init__(self, festivos: set[date] | None = None, sabados: bool = False):
        self.festivos = festivos or set()
        self.sabados = sabados

    def laborable(self, d: date) -> bool:
        if d.weekday() == 6 or (d.weekday() == 5 and not self.sabados):
            return False
        return d not in self.festivos

    def siguiente_laborable(self, d: date) -> date:
        while not self.laborable(d):
            d += timedelta(days=1)
        return d

    def sumar(self, d: date, n: int) -> date:
        """Día laborable n posiciones después de d (n puede ser negativo). d se ajusta antes al laborable siguiente."""
        d = self.siguiente_laborable(d) if n >= 0 else d
        paso = 1 if n >= 0 else -1
        k = 0
        while k < abs(n):
            d += timedelta(days=paso)
            if self.laborable(d):
                k += 1
        return d

    def indice(self, origen: date, d: date) -> int:
        """Nº de días laborables entre origen (índice 0) y d."""
        if d >= origen:
            return sum(1 for i in range((d - origen).days) if self.laborable(origen + timedelta(days=i)))
        return -sum(1 for i in range((origen - d).days) if self.laborable(d + timedelta(days=i)))


def parse_festivos(txt: str) -> set[date]:
    """«2026-08-03..2026-08-21, 2026-12-25, 25/12/2026». Rangos con «..»."""
    out = set()
    for t in re.split(r"[,;\n]+", txt or ""):
        t = t.strip()
        if not t:
            continue
        partes = [p.strip() for p in t.split("..")]
        try:
            fs = [_fecha(p) for p in partes]
        except ValueError:
            continue
        if len(fs) == 1:
            out.add(fs[0])
        else:
            d = fs[0]
            while d <= fs[1]:
                out.add(d)
                d += timedelta(days=1)
    return out


def _fecha(s) -> date:
    if isinstance(s, date):
        return s
    s = str(s).strip()[:10]
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$", s)
    if m:
        y = int(m.group(3))
        return date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    return date.fromisoformat(s)


# ============================================================================ dependencias
RE_DEP = re.compile(r"^\s*([A-Za-z0-9_.\-]+?)\s*(FC|CC|FF|FS|SS)?\s*([+-]\s*\d+)?\s*$", re.I)


def parse_predecesoras(txt: str) -> list[tuple[str, str, int]]:
    """«A10; A20CC+2; A30FF-1» → [(A10,FC,0), (A20,CC,2), (A30,FF,-1)]. Admite FS/SS (inglés)."""
    out = []
    for t in re.split(r"[;,]+", txt or ""):
        if not t.strip():
            continue
        m = RE_DEP.match(t)
        if not m:
            raise ValueError(f"Predecesora no válida: «{t.strip()}» (ejemplos: A10, A20CC+2, A30FF-1)")
        tipo = (m.group(2) or "FC").upper()
        tipo = {"FS": "FC", "SS": "CC"}.get(tipo, tipo)
        out.append((m.group(1), tipo, int(m.group(3).replace(" ", "")) if m.group(3) else 0))
    return out


def calcular(acts: list[dict], cal: Calendario, inicio: date, fecha_control: date | None = None) -> dict:
    """CPM sobre índices de días laborables. `acts`: dicts con codigo, duracion, predecesoras y (opcional) lo real.
    Con fecha_control, lo no terminado no puede empezar/terminar antes de ella, y lo real manda sobre lo previsto."""
    por_cod = {a["codigo"]: a for a in acts}
    deps = {}
    errores = []
    for a in acts:
        try:
            ds = parse_predecesoras(a.get("predecesoras") or "")
        except ValueError as e:
            errores.append(f"{a['codigo']}: {e}")
            ds = []
        for p, _, _ in ds:
            if p not in por_cod:
                errores.append(f"{a['codigo']}: la predecesora «{p}» no existe")
            if p == a["codigo"]:
                errores.append(f"{a['codigo']}: depende de sí misma")
        deps[a["codigo"]] = [d for d in ds if d[0] in por_cod and d[0] != a["codigo"]]
    # orden topológico (Kahn)
    ent = {c: 0 for c in por_cod}
    suc = defaultdict(list)
    for c, ds in deps.items():
        for p, t, l in ds:
            ent[c] += 1
            suc[p].append((c, t, l))
    cola = deque(sorted(c for c, n in ent.items() if n == 0))
    orden = []
    while cola:
        c = cola.popleft()
        orden.append(c)
        for s, _, _ in suc[c]:
            ent[s] -= 1
            if ent[s] == 0:
                cola.append(s)
    if len(orden) != len(por_cod):
        ciclo = sorted(set(por_cod) - set(orden))
        raise ValueError("Dependencias circulares entre: " + ", ".join(ciclo) + (". " + "; ".join(errores) if errores else ""))
    ic = cal.indice(inicio, fecha_control) if fecha_control else None
    ES, EF = {}, {}
    for c in orden:
        a = por_cod[c]
        dur = max(0, int(a.get("duracion") or 0))
        es = 0
        if a.get("inicio_min"):
            es = max(es, cal.indice(inicio, _fecha(a["inicio_min"])))
        for p, t, l in deps[c]:
            if t == "FC":
                es = max(es, EF[p] + l)
            elif t == "CC":
                es = max(es, ES[p] + l)
            elif t == "FF":
                es = max(es, EF[p] + l - dur)
        real_ini = a.get("inicio_real")
        real_fin = a.get("fin_real")
        if real_ini:
            es = cal.indice(inicio, _fecha(real_ini))
        if real_fin:
            ef = cal.indice(inicio, _fecha(real_fin)) + 1
            ES[c], EF[c] = es, max(es, ef)
            continue
        if ic is not None:
            if real_ini:
                resto = a.get("restante")
                if resto is None or resto == "":
                    resto = round(dur * (1 - (int(a.get("avance") or 0) / 100)))
                ES[c] = es
                EF[c] = max(ic, es) + max(0, int(resto))
                continue
            es = max(es, ic)                      # lo que no ha empezado no puede empezar antes de la fecha de control
        ES[c], EF[c] = es, es + dur
    fin = max(EF.values()) if EF else 0
    LS, LF = {}, {}
    for c in reversed(orden):
        a = por_cod[c]
        dur = EF[c] - ES[c]
        lf = fin
        for s, t, l in suc[c]:
            if t == "FC":
                lf = min(lf, LS[s] - l)
            elif t == "CC":
                lf = min(lf, LS[s] - l + dur)
            elif t == "FF":
                lf = min(lf, LF[s] - l)
        LF[c], LS[c] = lf, lf - dur
    res = []
    for c in orden:
        a = por_cod[c]
        hol = LS[c] - ES[c]
        ini = cal.sumar(inicio, ES[c])
        fin_d = cal.sumar(inicio, max(ES[c], EF[c] - 1)) if EF[c] > ES[c] else ini
        res.append({**{k: a.get(k) for k in ("codigo", "nombre", "responsable", "capitulo", "avance", "inicio_real", "fin_real", "predecesoras")},
                    "duracion": EF[c] - ES[c], "es": ES[c], "ef": EF[c], "ls": LS[c], "lf": LF[c], "holgura": hol, "critica": hol <= 0,
                    "inicio": ini, "fin": fin_d})
    orden_vis = {a["codigo"]: i for i, a in enumerate(acts)}
    res.sort(key=lambda r: orden_vis.get(r["codigo"], 0))
    return {"actividades": res, "fin_obra": cal.sumar(inicio, max(0, fin - 1)) if fin else inicio, "duracion_total": fin,
            "errores": errores}


# ============================================================================ datos de la obra
def config(con, obra_id: int) -> dict:
    r = db.one(con, "SELECT * FROM plan_config WHERE obra_id=?", (obra_id,))
    return r or {"obra_id": obra_id, "inicio_obra": date.today().isoformat(), "festivos": "", "sabados": 0, "fecha_control": None}


def guardar_config(con, obra_id: int, inicio_obra: str, festivos: str, sabados: bool, fecha_control: str | None, usuario: str) -> None:
    _fecha(inicio_obra)
    with db.tx(con):
        con.execute("""INSERT INTO plan_config (obra_id, inicio_obra, festivos, sabados, fecha_control) VALUES (?,?,?,?,?)
                       ON CONFLICT(obra_id) DO UPDATE SET inicio_obra=excluded.inicio_obra, festivos=excluded.festivos,
                       sabados=excluded.sabados, fecha_control=excluded.fecha_control""",
                    (obra_id, inicio_obra, festivos, int(bool(sabados)), fecha_control))
        db.audit(con, usuario, "plan_config", "obra", obra_id, {"inicio": inicio_obra, "control": fecha_control})


def calendario(con, obra_id: int) -> Calendario:
    c = config(con, obra_id)
    return Calendario(parse_festivos(c.get("festivos") or ""), bool(c.get("sabados")))


def actividades(con, obra_id: int) -> list[dict]:
    return db.rows(con, "SELECT * FROM plan_actividades WHERE obra_id=? ORDER BY orden, id", (obra_id,))


def guardar_actividades(con, obra_id: int, filas: list[dict], usuario: str) -> int:
    """Sustituye las actividades de la obra (validando antes que el planning se puede calcular)."""
    limpias = []
    codigos = set()
    for i, f in enumerate(filas, 1):
        cod = str(f.get("codigo") or "").strip()
        nom = str(f.get("nombre") or "").strip()
        if not cod and not nom:
            continue
        if not cod or not nom:
            raise ValueError(f"Fila {i}: faltan código o nombre.")
        if cod in codigos:
            raise ValueError(f"Código repetido: {cod}")
        codigos.add(cod)
        try:
            dur = int(float(str(f.get("duracion") or 0).replace(",", ".")))
        except ValueError:
            raise ValueError(f"{cod}: duración no válida")
        if dur < 0:
            raise ValueError(f"{cod}: duración negativa")
        av = f.get("avance")
        av = int(float(str(av).replace(",", "."))) if av not in (None, "") and str(av) != "nan" else 0
        if not 0 <= av <= 100:
            raise ValueError(f"{cod}: el avance debe estar entre 0 y 100")
        g = lambda k: (str(f.get(k)).strip() if f.get(k) not in (None, "") and str(f.get(k)) not in ("nan", "NaT", "None") else None)  # noqa: E731
        for k in ("inicio_real", "fin_real", "inicio_min"):
            if g(k):
                _fecha(g(k))
        rest = g("restante")
        limpias.append({"codigo": cod, "nombre": nom, "duracion": dur, "predecesoras": g("predecesoras") or "", "responsable": g("responsable"),
                        "capitulo": g("capitulo"), "inicio_min": g("inicio_min") and _fecha(g("inicio_min")).isoformat(),
                        "inicio_real": g("inicio_real") and _fecha(g("inicio_real")).isoformat(),
                        "fin_real": g("fin_real") and _fecha(g("fin_real")).isoformat(), "avance": 100 if g("fin_real") else av,
                        "restante": int(float(rest)) if rest else None, "notas": g("notas"), "orden": i})
    cfg = config(con, obra_id)
    calcular(limpias, calendario(con, obra_id), _fecha(cfg["inicio_obra"]))      # lanza error si hay ciclos
    with db.tx(con):
        con.execute("DELETE FROM plan_actividades WHERE obra_id=?", (obra_id,))
        for a in limpias:
            con.execute(f"INSERT INTO plan_actividades (obra_id, {', '.join(a)}) VALUES (?, {','.join('?' * len(a))})", [obra_id, *a.values()])
        db.audit(con, usuario, "plan_actividades", "obra", obra_id, {"actividades": len(limpias)})
    return len(limpias)


def planificar(con, obra_id: int, con_real: bool = True) -> dict:
    cfg = config(con, obra_id)
    acts = actividades(con, obra_id)
    if not con_real:
        acts = [{**a, "inicio_real": None, "fin_real": None, "avance": 0, "restante": None} for a in acts]
    fc = _fecha(cfg["fecha_control"]) if con_real and cfg.get("fecha_control") else None
    return calcular(acts, calendario(con, obra_id), _fecha(cfg["inicio_obra"]), fc)


def congelar_linea_base(con, obra_id: int, nombre: str, usuario: str) -> int:
    p = planificar(con, obra_id, con_real=False)
    datos = [{"codigo": a["codigo"], "nombre": a["nombre"], "inicio": a["inicio"].isoformat(), "fin": a["fin"].isoformat(),
              "duracion": a["duracion"]} for a in p["actividades"]]
    with db.tx(con):
        cur = con.execute("INSERT INTO plan_lineas_base (obra_id, nombre, creada_por, creada_en, datos) VALUES (?,?,?,?,?)",
                          (obra_id, nombre or f"Línea base {date.today().isoformat()}", usuario, db.now_iso(),
                           json.dumps({"actividades": datos, "fin_obra": p["fin_obra"].isoformat()})))
        db.audit(con, usuario, "plan_linea_base", "obra", obra_id, {"nombre": nombre, "fin": p["fin_obra"].isoformat()})
    return cur.lastrowid


def lineas_base(con, obra_id: int) -> list[dict]:
    return db.rows(con, "SELECT id, nombre, creada_por, creada_en FROM plan_lineas_base WHERE obra_id=? ORDER BY id DESC", (obra_id,))


def replanificar(con, obra_id: int, linea_base_id: int | None = None) -> dict:
    """Planning actual (con lo real a la fecha de control) frente a la línea base: desplazamientos y retraso de fin de obra."""
    actual = planificar(con, obra_id, con_real=True)
    lb = db.one(con, "SELECT * FROM plan_lineas_base WHERE id=?", (linea_base_id,)) if linea_base_id else \
        db.one(con, "SELECT * FROM plan_lineas_base WHERE obra_id=? ORDER BY id DESC LIMIT 1", (obra_id,))
    if not lb:
        return {"actual": actual, "base": None, "cambios": [], "retraso_fin_dias": None}
    base = json.loads(lb["datos"])
    bmap = {a["codigo"]: a for a in base["actividades"]}
    cal = calendario(con, obra_id)
    cambios = []
    for a in actual["actividades"]:
        b = bmap.get(a["codigo"])
        if not b:
            cambios.append({**a, "desplaz_inicio": None, "desplaz_fin": None, "motivo": "actividad nueva"})
            continue
        bi, bf = _fecha(b["inicio"]), _fecha(b["fin"])
        di = cal.indice(bi, a["inicio"])
        df = cal.indice(bf, a["fin"])
        if di or df:
            cambios.append({**a, "inicio_base": bi, "fin_base": bf, "desplaz_inicio": di, "desplaz_fin": df,
                            "motivo": "retraso propio" if a.get("inicio_real") or a.get("fin_real") else "arrastrado por predecesoras"})
    fin_base = _fecha(base["fin_obra"])
    return {"actual": actual, "base": {"id": lb["id"], "nombre": lb["nombre"], "fin_obra": fin_base}, "cambios": cambios,
            "retraso_fin_dias": cal.indice(fin_base, actual["fin_obra"])}


def retrasadas(con, obra_id: int, hoy: date | None = None) -> list[dict]:
    """Actividades que según lo previsto ya deberían haber empezado/terminado y no constan como tales."""
    hoy = hoy or date.today()
    try:
        p = planificar(con, obra_id, con_real=True)
    except Exception:
        return []
    out = []
    for a in p["actividades"]:
        if a.get("fin_real"):
            continue
        if a["fin"] < hoy:
            out.append({**a, "problema": "debía haber terminado"})
        elif a["inicio"] < hoy and not a.get("inicio_real"):
            out.append({**a, "problema": "debía haber empezado"})
    return out


def leer_excel(data: bytes, nombre: str = "") -> list[dict]:
    import pandas as pd
    df = pd.read_csv(io.BytesIO(data), sep=None, engine="python", dtype=str) if nombre.lower().endswith(".csv") else \
        pd.read_excel(io.BytesIO(data), dtype=str)
    norm = {c: re.sub(r"[^a-z]", "", str(c).lower().translate(str.maketrans("áéíóú", "aeiou"))) for c in df.columns}
    alias = {"codigo": ("codigo", "id", "cod"), "nombre": ("nombre", "actividad", "tarea", "descripcion", "nombredetarea"),
             "duracion": ("duracion", "dias", "duraciondias"), "predecesoras": ("predecesoras", "predecesores", "dependencias", "predecessors"),
             "responsable": ("responsable", "subcontrata", "empresa", "recurso", "nombresderecursos"), "capitulo": ("capitulo",),
             "inicio_min": ("iniciominimo", "noantesde"), "inicio_real": ("inicioreal",), "fin_real": ("finreal",), "avance": ("avance", "completado")}
    col = {}
    for k, ops in alias.items():
        for c, n in norm.items():
            if n in ops and c not in col.values():
                col[k] = c
                break
    if "nombre" not in col or "duracion" not in col:
        raise ValueError("La hoja necesita al menos columnas de actividad y duración.")
    out = []
    for i, r in enumerate(df.fillna("").to_dict("records"), 1):
        f = {k: r.get(c, "") for k, c in col.items()}
        f["codigo"] = f.get("codigo") or str(i)
        f["duracion"] = re.sub(r"[^\d.,]", "", str(f["duracion"])) or "0"
        if f.get("avance"):
            f["avance"] = re.sub(r"[^\d.,]", "", str(f["avance"])) or "0"
        out.append(f)
    return out
