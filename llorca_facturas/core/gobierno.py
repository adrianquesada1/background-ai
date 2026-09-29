"""
Gobierno de obra: memoria estructurada de cada obra.

Reuniones -> compromisos (qué, quién, para cuándo), decisiones y EXTRAS (trabajos adicionales o modificaciones).
- Las notas de la reunión (pegadas o escritas) se analizan con reglas: cada frase que implica una acción, una decisión o un
  posible extra se propone como elemento; la persona lo revisa antes de guardarlo.
- Los asuntos abiertos pasan a la reunión siguiente; los vencidos generan alerta.
- Un extra se ancla a un capítulo y genera un borrador de orden de cambio para valorar y formalizar.
Siempre con revisión humana: nada se emite sin que alguien lo confirme.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS obra_reuniones (
    id INTEGER PRIMARY KEY, obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    fecha TEXT, titulo TEXT, asistentes TEXT, notas TEXT, creado_por TEXT, creado_en TEXT);
CREATE TABLE IF NOT EXISTS obra_compromisos (
    id INTEGER PRIMARY KEY, obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    reunion_id INTEGER REFERENCES obra_reuniones(id) ON DELETE SET NULL,
    tipo TEXT DEFAULT 'compromiso', descripcion TEXT NOT NULL, responsable TEXT, fecha_limite TEXT,
    estado TEXT DEFAULT 'abierto', partida_id INTEGER, importe_estimado_cents INTEGER, notas TEXT,
    creado_por TEXT, creado_en TEXT, cerrado_por TEXT, cerrado_en TEXT);
"""
TIPOS = {"compromiso": "Compromiso", "decision": "Decisión", "extra": "Posible extra / modificación", "pendiente": "Tema pendiente",
         "posventa": "Incidencia de posventa", "juridico": "Asunto jurídico / abogado externo", "plano": "Plano / ficha técnica pendiente"}
ESTADOS = {"abierto": "Abierto", "cumplido": "Cumplido", "cancelado": "Cancelado", "valorado": "Extra valorado", "aprobado": "Extra aprobado"}

RE_EXTRA = re.compile(r"\b(extra|adicional|modificaci[oó]n|modificar|cambio de|cambiar|no inclu[ií]d|fuera de (presupuesto|proyecto)|"
                      r"orden de cambio|nuevo trabajo|ampliaci[oó]n|a mayores|sustituir|variaci[oó]n)", re.I)
RE_DECISION = re.compile(r"\b(se acuerda|se aprueba|se decide|queda aprobad|se valida|se opta|acordado)", re.I)
RE_COMPROMISO = re.compile(r"\b(se compromete|compromiso|deber[áa]|debe|queda pendiente|pendiente de|enviar[áa]?|revisar[áa]?|"
                           r"entregar[áa]?|confirmar[áa]?|presentar[áa]?|preparar[áa]?|antes del|para el d[ií]a|lo har[áa]|"
                           r"a cargo de|se encarga|traer[áa]?|solicitar[áa]?|coordinar[áa]?)", re.I)
MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
         "octubre": 10, "noviembre": 11, "diciembre": 12}


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def _fecha(frase: str, ref: date) -> str | None:
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", frase)
    if m:
        y = int(m.group(3)) if m.group(3) else ref.year
        y = y + 2000 if y < 100 else y
        try:
            return date(y, int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2}) de ([a-z]+)", frase.lower())
    if m and m.group(2) in MESES:
        try:
            return date(ref.year, MESES[m.group(2)], int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def _responsable(frase: str) -> str | None:
    for pat in (r"responsable\s*:\s*([A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)?)",
                r"a cargo de\s+([A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)?)",
                r"([A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)?)\s+(?:se compromete|se encarga|enviar[áa]|revisar[áa]|"
                r"entregar[áa]|confirmar[áa]|presentar[áa]|preparar[áa]|traer[áa]|solicitar[áa]|coordinar[áa])",
                r"\(([A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)?)\)",
                r"\b(DF|D\.F\.|Direcci[oó]n Facultativa|Propiedad|Llorca|Constructora)\b"):
        m = re.search(pat, frase)
        if m and m.group(1).lower() not in ("se", "el", "la", "los", "las", "queda", "debe"):
            return m.group(1)
    return None


def analizar_notas(texto: str, ref: date | None = None) -> list[dict]:
    """Propuestas de compromisos, decisiones y extras a partir de las notas. Nada se guarda sin revisión."""
    ref = ref or date.today()
    # frases: por líneas y por punto seguido de mayúscula (no parte «aprox. 3.500 €» ni «Sr. García»)
    frases = [f.strip(" -•*\t") for f in re.split(r"[\n\r]+|(?<=[a-záéíóúñ0-9)][.;])\s+(?=[A-ZÁÉÍÓÚÑ])", texto or "")
              if len(f.strip()) > 8]
    out = []
    for f in frases:
        tipo = "extra" if RE_EXTRA.search(f) else "decision" if RE_DECISION.search(f) else "compromiso" if RE_COMPROMISO.search(f) else None
        if not tipo:
            continue
        imp = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*(?:€|euros)", f)
        out.append({"tipo": tipo, "descripcion": f[:300], "responsable": _responsable(f), "fecha_limite": _fecha(f, ref),
                    "importe": imp.group(1) if imp else None})
    return out


def abiertos(con, obra_id: int) -> list[dict]:
    return db.rows(con, """SELECT c.*, r.fecha AS fecha_reunion FROM obra_compromisos c LEFT JOIN obra_reuniones r ON r.id=c.reunion_id
                           WHERE c.obra_id=? AND c.estado='abierto' ORDER BY COALESCE(c.fecha_limite,'9999'), c.id""", (obra_id,))


def vencidos(con, obra_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM obra_compromisos WHERE estado='abierto' AND fecha_limite IS NOT NULL AND fecha_limite < ?"
    p = [date.today().isoformat()]
    if obra_id:
        q += " AND obra_id=?"; p.append(obra_id)
    return db.rows(con, q, p)


def acta_html(con, reunion_id: int, tipo_acta: str = "interna") -> str:
    r = db.one(con, "SELECT r.*, o.codigo, o.nombre FROM obra_reuniones r JOIN obras o ON o.id=r.obra_id WHERE r.id=?", (reunion_id,))
    items = db.rows(con, """SELECT c.*, p.codigo AS cap FROM obra_compromisos c LEFT JOIN partidas p ON p.id=c.partida_id
                            WHERE c.reunion_id=? ORDER BY c.tipo, c.id""", (reunion_id,))
    prev = db.rows(con, "SELECT * FROM obra_compromisos WHERE obra_id=? AND estado='abierto' AND (reunion_id IS NULL OR reunion_id<>?) "
                        "ORDER BY fecha_limite", (r["obra_id"], reunion_id))

    def lista(filas, extra=False):
        if not filas:
            return "<p><i>Ninguno.</i></p>"
        return "<ol>" + "".join(
            f"<li>{html.escape(x['descripcion'])}"
            + (f" — <b>Responsable:</b> {html.escape(x['responsable'])}" if x.get("responsable") else "")
            + (f" — <b>Plazo:</b> {x['fecha_limite']}" if x.get("fecha_limite") else "")
            + (f" — <b>Capítulo:</b> {x['cap']}" if extra and x.get("cap") else "")
            + (f" — <b>Estimación:</b> {x['importe_estimado_cents'] / 100:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
               if extra and x.get("importe_estimado_cents") else "") + "</li>" for x in filas) + "</ol>"
    titulo = "ACTA DE REUNIÓN DE OBRA" + (" — DIRECCIÓN FACULTATIVA" if tipo_acta == "df" else " — ACTA INTERNA LLORCA")
    extras = [x for x in items if x["tipo"] == "extra"]
    cuerpo = (f"<h2>Asistentes</h2><p>{html.escape(r['asistentes'] or '—')}</p>"
              f"<h2>Decisiones</h2>{lista([x for x in items if x['tipo'] == 'decision'])}"
              f"<h2>Compromisos</h2>{lista([x for x in items if x['tipo'] in ('compromiso', 'pendiente')])}")
    if tipo_acta == "interna":
        cuerpo += (f"<h2>Posibles extras y modificaciones (a valorar)</h2>{lista(extras, True)}"
                   f"<h2>Asuntos abiertos de reuniones anteriores</h2>{lista(prev)}")
    else:
        cuerpo += f"<h2>Modificaciones solicitadas</h2>{lista(extras)}"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Acta {r['fecha']}</title>
<style>body{{font-family:'Segoe UI',Arial,sans-serif;color:#2E2E2E;font-size:11pt;margin:18mm}} h1{{font-size:16pt;border-left:6px solid #E1251B;
padding-left:10px}} h2{{font-size:12pt;border-bottom:1px solid #ddd}} .sub{{color:#777}} li{{margin-bottom:5px}}</style></head><body>
<h1>{titulo}</h1><div class="sub">Obra {html.escape(r['codigo'])} · {html.escape(r['nombre'])} · {r['fecha']} · {html.escape(r['titulo'] or '')}</div>
{cuerpo}<p class="sub" style="margin-top:30px">Borrador generado por el sistema a partir de las notas de la reunión. Requiere revisión y
firma antes de su emisión.</p></body></html>"""


def orden_cambio_html(con, compromiso_id: int) -> str:
    c = db.one(con, """SELECT c.*, o.codigo, o.nombre, p.codigo AS cap, p.descripcion AS capitulo FROM obra_compromisos c
                       JOIN obras o ON o.id=c.obra_id LEFT JOIN partidas p ON p.id=c.partida_id WHERE c.id=?""", (compromiso_id,))
    imp = f"{c['importe_estimado_cents'] / 100:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if c["importe_estimado_cents"] else "pendiente de valorar"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Orden de cambio</title>
<style>body{{font-family:'Segoe UI',Arial,sans-serif;color:#2E2E2E;margin:18mm}} h1{{border-left:6px solid #E1251B;padding-left:10px}}
td{{padding:6px;border-bottom:1px solid #ddd}}</style></head><body><h1>BORRADOR DE ORDEN DE CAMBIO</h1>
<table><tr><td><b>Obra</b></td><td>{html.escape(c['codigo'])} · {html.escape(c['nombre'])}</td></tr>
<tr><td><b>Origen</b></td><td>Reunión de obra{(' del ' + str(c['creado_en'])[:10]) if c['creado_en'] else ''}</td></tr>
<tr><td><b>Descripción</b></td><td>{html.escape(c['descripcion'])}</td></tr>
<tr><td><b>Capítulo afectado</b></td><td>{html.escape((c['cap'] or '') + ' ' + (c['capitulo'] or 'sin asignar'))}</td></tr>
<tr><td><b>Solicitado por</b></td><td>{html.escape(c['responsable'] or '—')}</td></tr>
<tr><td><b>Importe estimado</b></td><td>{imp}</td></tr></table>
<p>Pendiente de valoración por Estudios y aprobación de la Propiedad antes de ejecutar. Lo que no se escribe no se cobra.</p>
</body></html>"""
