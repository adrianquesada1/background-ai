"""
RESULTADO DE OBRA POR OFICIO.

Razonamiento (el mismo con el que la empresa analiza sus obras):
- La venta está organizada por CAPÍTULOS de la certificación; el coste llega por INDUSTRIAL (proveedor).
  El puente entre ambos es el OFICIO (estructura, albañilería, pladur, climatización y fontanería…):
  cada partida certificada pertenece a un oficio y cada proveedor trabaja en un oficio.
- La venta incluye el PASE (gastos generales + beneficio). Para comparar con el coste se usa la venta sin pase.
- En un oficio en curso, lo cobrado por delante de lo pagado no es beneficio: es coste que aún llegará.

Fuentes de datos (todas opcionales salvo la certificación):
- Certificación del cliente (PDF) -> venta.
- Facturas registradas en la app y/o exportación de compras de SIS -> coste.
- Partes de trabajo de SIS -> personal propio.
Los oficios de partidas y proveedores se proponen AUTOMÁTICAMENTE y se aprenden de lo que el usuario confirma.

Detalle del cálculo:

1. VENTAS SIS   = certificado a origen al cliente.
   COMPRAS SIS  = Σ «Total Base» de las facturas de compra de la obra en SIS.
   RRHH SIS     = Σ «Total Coste» de los partes de trabajo del personal propio.
   RESULTADO SIS = VENTAS − COMPRAS − RRHH.

2. Cada partida de la certificación tiene un CRITERIO: la categoría de coste a la que pertenece
   (estructura, albañilería, pladur…). Cada proveedor de compras tiene también su categoría.
   Por categoría:
       COBRADO     = Σ certificado a origen de sus partidas
       SIN PASE    = COBRADO / (1 + PASE)      (PASE = coeficiente GG+BI incluido en la venta; p.ej. 16 %)
       PAGADO      = Σ compras de sus proveedores
       DIFERENCIAL = SIN PASE − PAGADO

3. CORRECCIÓN = − Σ DIFERENCIAL de las categorías EN CURSO. En una categoría que no ha terminado,
   lo cobrado por delante de lo pagado no es beneficio: son costes que aún llegarán. Por prudencia se
   supone que su coste final será la venta sin pase. Las categorías TERMINADAS no se corrigen: su
   diferencial ya es real.
   RESULTADO = RESULTADO SIS + CORRECCIÓN.

4. PROYECCIÓN a fin de obra = RESULTADO HOY + beneficio por indirectos (presupuesto indirecto pendiente
   − coste indirecto estimado) − desviaciones de coste (% del presupuesto) + GG de la facturación pendiente.

Todo se calcula con Decimal (exacto) y se guarda como texto decimal.
"""
from __future__ import annotations

import io
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
from rapidfuzz import fuzz, process

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS aud_parametros (
    obra_id INTEGER PRIMARY KEY REFERENCES obras(id) ON DELETE CASCADE,
    pase TEXT DEFAULT '0.16', ppto_venta TEXT DEFAULT '0', cd TEXT DEFAULT '0', ci TEXT DEFAULT '0', gg TEXT DEFAULT '0',
    desv_pct TEXT DEFAULT '1', ind_estimado TEXT DEFAULT '0', ind_pendiente TEXT DEFAULT '0',
    actualizado_por TEXT, actualizado_en TEXT
);
CREATE TABLE IF NOT EXISTS aud_categorias (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    nombre TEXT NOT NULL,           -- nombre de la categoría de coste (como en «CAT COSTE»)
    nombre_venta TEXT,              -- columna de criterio en la certificación
    tipo TEXT DEFAULT 'DIRECTO',
    terminada INTEGER DEFAULT 0,
    orden INTEGER,
    UNIQUE (obra_id, nombre)
);
CREATE TABLE IF NOT EXISTS aud_proveedor_cat (
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    proveedor_norm TEXT NOT NULL, proveedor TEXT,
    categoria_id INTEGER REFERENCES aud_categorias(id) ON DELETE SET NULL,
    origen TEXT DEFAULT 'excel',
    PRIMARY KEY (obra_id, proveedor_norm)
);
CREATE TABLE IF NOT EXISTS aud_criterios (
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    clave TEXT NOT NULL, codigo TEXT, titulo TEXT,
    categoria_id INTEGER REFERENCES aud_categorias(id) ON DELETE SET NULL,
    origen TEXT DEFAULT 'excel',
    PRIMARY KEY (obra_id, clave)
);
CREATE TABLE IF NOT EXISTS compras_sis (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    proveedor TEXT, proveedor_norm TEXT, num_factura TEXT, num_entrada TEXT,
    f_factura TEXT, f_contable TEXT, anio INTEGER, mes INTEGER,
    base TEXT, total TEXT, cuota_iva TEXT, ret_gar TEXT, pendiente_real TEXT,
    isp INTEGER, aprobada INTEGER, jefe_obra TEXT, forma_pago TEXT, lote TEXT
);
CREATE INDEX IF NOT EXISTS ix_csis_obra ON compras_sis(obra_id);
CREATE TABLE IF NOT EXISTS rrhh_sis (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    fecha TEXT, empleado TEXT, nombre TEXT, horas TEXT, precio TEXT, coste TEXT, categoria TEXT,
    mes INTEGER, anio INTEGER, lote TEXT
);
CREATE INDEX IF NOT EXISTS ix_rsis_obra ON rrhh_sis(obra_id);
CREATE TABLE IF NOT EXISTS aud_historico (
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    mes TEXT NOT NULL,              -- YYYY-MM
    ventas TEXT, compras TEXT, rrhh TEXT, correccion TEXT, fuente TEXT,
    PRIMARY KEY (obra_id, mes)
);
"""

MESES = ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE",
         "NOVIEMBRE", "DICIEMBRE"]
Z = Decimal(0)


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def D(v) -> Decimal:
    if v is None or v == "":
        return Z
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(repr(v)) if abs(v) > 1e-9 else Z
    return Decimal(str(v))


def r2(v) -> Decimal:
    return D(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9/ ]", " ", s)).strip()


def clave_partida(codigo, titulo) -> str:
    return f"{str(codigo or '').strip().upper()}|{norm(titulo)[:28]}"


# ============================================================================ oficios estándar
OFICIOS = [
    ("movimiento de tierras", "DIRECTO", "desbroce desmonte explanado excavacion excav relleno rell terraplen transporte tierras zanja pozos vaciado mov tierras"),
    ("estructura", "DIRECTO", "hormigon ha-30 hl-150 zapata zapatas forjado pilar pilares viga losa muro muros encofrado ferralla acero b500 solera gunita estructura"),
    ("albañilería", "DIRECTO", "ladrillo bloque fabrica tabique tabicon recrecido mortero enfoscado peldaño peldañeado vierteaguas albardilla remate remates ayudas albañileria monocapa"),
    ("impermeabilización y cubiertas", "DIRECTO", "impermeabilizacion impermeabilizante lamina cubierta aislamiento xps poliestireno lana mineral"),
    ("pladur", "DIRECTO", "placa yeso laminado pladur falso techo trasdosado autoportante aquapanel tabica registrable"),
    ("solados y alicatados", "DIRECTO", "solado alicatado gres porcelanico rodapie pavimento pav antid aplacado granito marmol baldosa ceramica tiles azulejo"),
    ("carpintería exterior y cerrajería", "DIRECTO", "ventana balconera aluminio carpinteria exterior barandilla cerrajeria composite persiana puerta cochera oscilo corredera"),
    ("vidrios", "DIRECTO", "vidrio acristalamiento climalit"),
    ("carpintería de madera", "DIRECTO", "puerta paso armario carpinteria madera tablero frente armario"),
    ("climatización y fontanería", "DIRECTO", "fontaneria saneamiento tuberia climatizacion aerotermia conducto ventilacion extraccion acs deposito bajante desague colector fancoil recuperador"),
    ("aparatos sanitarios", "DIRECTO", "inodoro lavabo plato ducha griferia mampara sanitarios bide"),
    ("electricidad y telecomunicaciones", "DIRECTO", "electrica electricidad iluminacion luminaria mecanismo cuadro cable antena telecomunicaciones domotica videoportero punto luz"),
    ("protección contra incendios", "DIRECTO", "extintor bie detector humo pci incendios incendio"),
    ("pintura", "DIRECTO", "pintura pintado esmalte plastica papel pintado"),
    ("cocinas", "DIRECTO", "cocina cocinas encimera electrodomestico campana"),
    ("ascensores", "DIRECTO", "ascensor ascensores elevador"),
    ("piscina", "DIRECTO", "piscina depuradora vaso depuracion"),
    ("urbanización y jardinería", "DIRECTO", "jardin jardineria cesped riego arbol vegetacion pergola juegos infantiles urbanizacion"),
    ("gestión de residuos", "DIRECTO", "residuo residuos escombro contenedor rcd"),
    ("seguridad y salud", "DIRECTO", "seguridad salud proteccion colectiva epi señalizacion"),
    ("control de calidad", "DIRECTO", "ensayo ensayos control calidad laboratorio"),
    ("grúa y maquinaria", "INDIRECTO", "grua maquinaria plataforma elevadora"),
    ("andamios y medios auxiliares", "INDIRECTO", "andamio andamios medios auxiliares"),
    ("alquileres e instalaciones de obra", "INDIRECTO", "alquiler caseta modulo wc quimico"),
    ("personal técnico y dirección", "INDIRECTO", "jefe obra arquitecto tecnico topografo topografia honorarios asistencia tecnica"),
    ("consumibles, portes y varios", "INDIRECTO", "herramienta herramientas portes mensajeria limpieza consumibles"),
]


def asegurar_oficios(con, obra_id: int) -> None:
    """Crea el catálogo estándar si la obra aún no tiene oficios (el usuario puede renombrar, añadir o importar los suyos)."""
    if db.one(con, "SELECT COUNT(*) n FROM aud_categorias WHERE obra_id=?", (obra_id,))["n"]:
        return
    with db.tx(con):
        for i, (n, t, _) in enumerate(OFICIOS):
            con.execute("INSERT INTO aud_categorias (obra_id, nombre, nombre_venta, tipo, orden) VALUES (?,?,?,?,?)",
                        (obra_id, n, n if t == "DIRECTO" else None, t, i))
    con.execute("INSERT OR IGNORE INTO aud_parametros (obra_id) VALUES (?)", (obra_id,))
    con.commit()


def _kw(con, obra_id):
    """Palabras clave por oficio: las del catálogo + las aprendidas de las partidas ya clasificadas."""
    cats = categorias(con, obra_id)
    base = {norm(n): k for n, _, k in OFICIOS}
    out = {}
    for c in cats:
        pal = set(norm(base.get(norm(c["nombre"]), "")).lower().split()) | set(norm(c["nombre"]).lower().split())
        out[c["id"]] = {w for w in pal if len(w) >= 3}
    return out


def _puntua(texto: str, kws: dict) -> tuple[int | None, float]:
    t = set(re.findall(r"[a-z0-9ñ\-]{3,}", norm(texto).lower()))
    t |= {w[:-1] for w in t if w.endswith("s")}
    mejor, ms, seg = None, 0, 0
    for cid, pal in kws.items():
        sc = len(t & pal)
        if sc > ms:
            mejor, seg, ms = cid, ms, sc
        elif sc > seg:
            seg = sc
    if not mejor:
        return None, 0.0
    return mejor, min(0.9, 0.35 + 0.15 * ms + 0.1 * (ms - seg))


def clasificar_partidas_auto(con, obra_id: int, cert_id: int) -> int:
    """Propone oficio a las partidas sin criterio: palabras clave del título + contexto del (sub)capítulo, suavizado por subcapítulo."""
    asegurar_oficios(con, obra_id)
    kws = {k: v for k, v in _kw(con, obra_id).items()
           if next((c for c in categorias(con, obra_id) if c["id"] == k), {}).get("tipo") == "DIRECTO"}
    caps = {r["codigo"]: r["nombre"] for r in db.rows(con, "SELECT codigo, nombre FROM cert_capitulos WHERE cert_id=?", (cert_id,))}
    lin = criterios_cert(con, obra_id, cert_id)
    if lin.empty:
        return 0
    nuevas = lin[lin["categoria_id"].isna()].copy()
    props = {}
    for r in nuevas.itertuples():
        texto = f"{r.titulo} {caps.get(r.subcapitulo, '')} {caps.get(r.capitulo, '')}"
        cid, conf = _puntua(texto, kws)
        if cid:
            props[r.Index] = cid
    nuevas["prop"] = pd.Series(props)
    # suavizado: dentro de un subcapítulo manda el oficio mayoritario (por importe)
    for sub, g in nuevas.groupby("subcapitulo"):
        if sub and g["prop"].notna().any():
            peso = g.dropna(subset=["prop"]).groupby("prop")["origen_m"].apply(lambda s_: s_.abs().sum())
            dom = peso.idxmax()
            if peso[dom] >= 0.6 * peso.sum():
                nuevas.loc[g.index, "prop"] = nuevas.loc[g.index, "prop"].fillna(dom)
    filas = [{"codigo": r.codigo, "titulo": r.titulo, "categoria_id": int(r.prop)} for r in nuevas.itertuples() if pd.notna(r.prop)]
    return guardar_criterios(con, obra_id, filas, "sistema", "auto") if filas else 0


def clasificar_proveedores_auto(con, obra_id: int) -> int:
    """Oficio de cada proveedor a partir del contenido de sus facturas (ponderado por importe). No pisa lo manual/importado."""
    asegurar_oficios(con, obra_id)
    kws = _kw(con, obra_id)
    lin = db.rows(con, """SELECT COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, l.descripcion, ABS(l.importe_cents) AS imp
                          FROM lineas l JOIN documentos d ON d.id=l.documento_id LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
                          WHERE d.obra_id=? AND d.estado<>'rechazada'""", (obra_id,))
    votos = defaultdict(Counter)
    for l in lin:
        cid, _ = _puntua(f"{l['descripcion']} {l['proveedor']}", kws)
        if cid and l["proveedor"]:
            votos[l["proveedor"]][cid] += l["imp"] or 1
    for r in db.rows(con, "SELECT DISTINCT proveedor FROM compras_sis WHERE obra_id=?", (obra_id,)):
        if r["proveedor"] not in votos:
            cid, _ = _puntua(r["proveedor"], kws)
            if cid:
                votos[r["proveedor"]][cid] += 1
    existentes = {r["proveedor_norm"] for r in db.rows(con, "SELECT proveedor_norm FROM aud_proveedor_cat WHERE obra_id=? "
                                                             "AND origen IN ('manual','excel')", (obra_id,))}
    n = 0
    with db.tx(con):
        for prov, c in votos.items():
            if norm(prov) in existentes:
                continue
            con.execute("INSERT INTO aud_proveedor_cat (obra_id, proveedor_norm, proveedor, categoria_id, origen) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(obra_id, proveedor_norm) DO UPDATE SET categoria_id=excluded.categoria_id WHERE origen='auto'",
                        (obra_id, norm(prov), prov, c.most_common(1)[0][0], "auto"))
            n += 1
    return n


# ============================================================================ importación de datos
def _hoja(wb, *prefijos):
    for p in prefijos:
        for n in wb.sheetnames:
            if norm(n) == norm(p):
                return n
    for p in prefijos:
        for n in wb.sheetnames:
            if norm(n).startswith(norm(p)):
                return n
    return None


def _tabla_sis(ws_vals, cabeceras_obligatorias: tuple[str, ...]):
    """Localiza la fila de cabecera de una exportación de SIS y devuelve (cabecera, filas)."""
    filas = list(ws_vals.iter_rows(values_only=True))
    for i, f in enumerate(filas[:15]):
        cab = [str(c).strip() if c is not None else "" for c in f]
        if all(any(norm(h) == norm(c) for c in cab) for h in cabeceras_obligatorias):
            return cab, filas[i + 1:]
    return None, []


def importar_compras(con, obra_id: int, ws, lote: str) -> dict:
    cab, filas = _tabla_sis(ws, ("Nombre", "Total Base"))
    if not cab:
        return {"compras": 0}
    ix = {norm(c): i for i, c in enumerate(cab)}
    g = lambda f, k: f[ix[norm(k)]] if norm(k) in ix and ix[norm(k)] < len(f) else None  # noqa: E731
    n, total = 0, Z
    with db.tx(con):
        con.execute("DELETE FROM compras_sis WHERE obra_id=?", (obra_id,))   # la exportación de SIS es acumulada a origen
        for f in filas:
            prov, base = g(f, "Nombre"), g(f, "Total Base")
            if not prov or not isinstance(base, (int, float, Decimal)) or g(f, "Proyecto") in (None, ""):
                continue                                                      # fuera filas de totales
            fc = g(f, "F. Contable") or g(f, "F. Factura")
            con.execute("""INSERT INTO compras_sis (obra_id, proveedor, proveedor_norm, num_factura, num_entrada, f_factura,
                           f_contable, anio, mes, base, total, cuota_iva, ret_gar, pendiente_real, isp, aprobada, jefe_obra,
                           forma_pago, lote) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (obra_id, str(prov).strip(), norm(prov), str(g(f, "Núm. Factura") or ""), str(g(f, "Núm. Entrada") or ""),
                         _fecha(g(f, "F. Factura")), _fecha(fc), _int(g(f, "Año")), _int(g(f, "Mes")),
                         str(r2(base)), str(r2(g(f, "Total"))), str(r2(g(f, "Total Cuota IVA"))), str(r2(g(f, "Ret. Gar."))),
                         str(r2(g(f, "Pendiente Real"))), int(bool(g(f, "I. Sujeto Pasivo"))), int(bool(g(f, "Aprobada"))),
                         str(g(f, "Nombre Empleado") or ""), str(g(f, "Forma Pago") or ""), lote))
            n += 1
            total += r2(base)
    return {"compras": n, "compras_total": total}


def importar_rrhh(con, obra_id: int, ws, lote: str) -> dict:
    cab, filas = _tabla_sis(ws, ("Nombre Empleado", "Total Coste"))
    if not cab:
        return {"rrhh": 0}
    ix = {norm(c): i for i, c in enumerate(cab)}
    g = lambda f, k: f[ix[norm(k)]] if norm(k) in ix and ix[norm(k)] < len(f) else None  # noqa: E731
    n, total = 0, Z
    with db.tx(con):
        con.execute("DELETE FROM rrhh_sis WHERE obra_id=?", (obra_id,))
        for f in filas:
            coste = g(f, "Total Coste")
            if not isinstance(coste, (int, float, Decimal)) or g(f, "Proyecto") in (None, "") or not g(f, "Nombre Empleado"):
                continue
            con.execute("""INSERT INTO rrhh_sis (obra_id, fecha, empleado, nombre, horas, precio, coste, categoria, mes, anio, lote)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (obra_id, _fecha(g(f, "Fecha")), str(g(f, "Empleado") or ""), str(g(f, "Nombre Empleado")),
                         str(D(g(f, "Horas"))), str(D(g(f, "Pre. Coste"))), str(r2(coste)), str(g(f, "Cat. Coste Descr.") or
                                                                                           g(f, "Cat. Coste Descr") or ""),
                         _int(g(f, "Mes")), _int(g(f, "Año")), lote))
            n += 1
            total += r2(coste)
    return {"rrhh": n, "rrhh_total": total}


def _fecha(v):
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    return None


def _int(v):
    try:
        return int(v)
    except Exception:
        return None


def importar_libro(con, obra_id: int, data: bytes, filename: str, usuario: str, cert_id: int | None = None) -> dict:
    """
    Importa un libro con cualquiera de estas hojas (o una exportación suelta de SIS):
      AUDITORIA · CERTIFICACIÓN <MES> (criterios) · CAT COSTE · COMPRAS SIS · RRHH SIS
    """
    from openpyxl import load_workbook
    wv = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    wf = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    res, avisos = {}, []
    lote = f"{filename} · {datetime.now():%Y-%m-%d %H:%M}"

    # ---------------------------------------------------------------- compras / rrhh (también exportación suelta)
    for n in wv.sheetnames:
        ws = wv[n]
        cab, _ = _tabla_sis(ws, ("Nombre", "Total Base"))
        if cab:
            res.update(importar_compras(con, obra_id, ws, lote))
            continue
        cab, _ = _tabla_sis(ws, ("Nombre Empleado", "Total Coste"))
        if cab:
            res.update(importar_rrhh(con, obra_id, ws, lote))

    # ---------------------------------------------------------------- categorías y proveedores (CAT COSTE)
    h = _hoja(wv, "CAT COSTE")
    if h:
        filas = list(wv[h].iter_rows(values_only=True))
        cats_tipo = {}
        prov_cat = []
        for f in filas:
            if len(f) >= 4 and f[0] and f[2] and f[3] and str(f[3]).upper() in ("DIRECTO", "INDIRECTO"):
                prov_cat.append((str(f[0]).strip(), str(f[2]).strip(), str(f[3]).upper()))
                cats_tipo[str(f[2]).strip()] = str(f[3]).upper()
        with db.tx(con):
            for i, (c, t) in enumerate(sorted(cats_tipo.items(), key=lambda x: (x[1], x[0]))):
                con.execute("INSERT INTO aud_categorias (obra_id, nombre, tipo, orden) VALUES (?,?,?,?) "
                            "ON CONFLICT(obra_id, nombre) DO UPDATE SET tipo=excluded.tipo", (obra_id, c, t, 100 + i))
            ids = {r["nombre"]: r["id"] for r in db.rows(con, "SELECT id, nombre FROM aud_categorias WHERE obra_id=?", (obra_id,))}
            for p, c, t in prov_cat:
                con.execute("INSERT INTO aud_proveedor_cat (obra_id, proveedor_norm, proveedor, categoria_id, origen) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(obra_id, proveedor_norm) DO UPDATE SET categoria_id=excluded.categoria_id, proveedor=excluded.proveedor",
                            (obra_id, norm(p), p, ids[c], "excel"))
        res["proveedores_clasificados"] = len(prov_cat)
        res["categorias"] = len(cats_tipo)

    # ---------------------------------------------------------------- AUDITORIA: parámetros, parejas venta↔coste, estado, histórico
    h = _hoja(wv, "AUDITORIA", "AUDITORÍA")
    if h:
        res.update(_importar_auditoria(con, obra_id, wv[h], wf[h], wv, avisos))

    # ---------------------------------------------------------------- criterios (hoja de certificación con columnas de categoría)
    h = _hoja(wv, "CERTIFICACION")
    if h:
        res.update(_importar_criterios(con, obra_id, wv[h], cert_id, avisos))
    db.audit(con, usuario, "importar_hoja_auditoria", "obra", obra_id, {"archivo": filename, **{k: str(v) for k, v in res.items()}})
    con.commit()
    res["avisos"] = avisos
    return res


def _importar_auditoria(con, obra_id, wsv, wsf, wv, avisos) -> dict:
    vals = {}
    for fila in wsv.iter_rows(min_row=1, max_row=400):
        for c in fila:
            if c.value is not None and hasattr(c, "coordinate"):
                vals[c.coordinate] = c.value
    forms = {}
    for fila in wsf.iter_rows(min_row=1, max_row=400):
        for c in fila:
            if isinstance(c.value, str) and c.value.startswith("=") and hasattr(c, "coordinate"):
                forms[c.coordinate] = c.value
    out = {}
    # parámetros: PASE junto a la etiqueta; presupuesto y CD/CI/GG por etiqueta en la columna A
    pase = None
    for k, v in vals.items():
        if isinstance(v, str) and norm(v) == "PASE":
            col, fila = re.match(r"([A-Z]+)(\d+)", k).groups()
            from openpyxl.utils import column_index_from_string, get_column_letter
            for d in (1, 2):
                vv = vals.get(f"{get_column_letter(column_index_from_string(col) + d)}{fila}")
                if isinstance(vv, (int, float)):
                    pase = vv
                    break
    etq = {norm(v): k for k, v in vals.items() if isinstance(v, str) and re.match(r"^A\d+$", k)}

    def valor(etiqueta, col="B"):
        k = next((kk for e, kk in etq.items() if e.startswith(norm(etiqueta))), None)
        return vals.get(col + k[1:]) if k else None
    p = {"pase": pase, "ppto_venta": valor("TOTAL PPTO VENTA"), "cd": valor("CD"), "ci": valor("CI"), "gg": valor("GG"),
         "ind_estimado": valor("ANALISIS INDIRECTO", "D"), "ind_pendiente": valor("PENDIENTE DE CONSU", "D")}
    dv = forms.get("D" + next((kk for e, kk in etq.items() if e.startswith("DESVIACIONES")), "A0")[1:], "")
    m = re.search(r"-?\s*(0?\.\d+)\s*\*", dv or "")
    p["desv_pct"] = Decimal(m.group(1)) * 100 if m else Decimal(1)
    with db.tx(con):
        con.execute("INSERT OR IGNORE INTO aud_parametros (obra_id) VALUES (?)", (obra_id,))
        for k, v in p.items():
            if v is not None:
                con.execute(f"UPDATE aud_parametros SET {k}=? WHERE obra_id=?", (str(D(v)), obra_id))
    out["parametros"] = {k: str(v) for k, v in p.items() if v is not None}

    # parejas: «COBRADO X» (=CERTIFICACIÓN!<col>13) y «PAGADO X» (=GETPIVOTDATA(... "Categoria","<cat>" ...))
    hc = _hoja(wv, "CERTIFICACION")
    cab_cert = {}
    if hc:
        fila12 = None
        for i, f in enumerate(wv[hc].iter_rows(min_row=1, max_row=20, values_only=True), start=1):
            if f and sum(1 for x in f if isinstance(x, str)) >= 8 and i > 5:
                fila12 = (i, f)
                break
        if fila12:
            from openpyxl.utils import get_column_letter
            cab_cert = {get_column_letter(j + 1): str(x).strip() for j, x in enumerate(fila12[1]) if isinstance(x, str) and j >= 15}
    parejas = []
    for k, v in vals.items():
        if isinstance(v, str) and norm(v).startswith("COBRADO"):
            col, fila = re.match(r"([A-Z]+)(\d+)", k).groups()
            fila = int(fila)
            f_cob = forms.get(f"{col}{fila + 1}", "")
            mcol = re.search(r"!\$?([A-Z]+)\$?\d+", f_cob)
            venta = cab_cert.get(mcol.group(1)) if mcol else None
            coste, terminada = None, False
            for dfila in range(3, 8):
                if isinstance(vals.get(f"{col}{fila + dfila}"), str) and norm(vals[f"{col}{fila + dfila}"]).startswith("PAGADO"):
                    fp = forms.get(f"{col}{fila + dfila + 1}", "")
                    mc = re.search(r'"Categoria"\s*,\s*"([^"]+)"', fp)
                    coste = mc.group(1) if mc else None
                    diff = vals.get(f"{col}{fila + dfila + 3}")
                    terminada = diff is None     # la hoja deja el diferencial en blanco en las partidas terminadas
                    break
            parejas.append({"etiqueta": v.replace("COBRADO", "").strip(), "venta": venta, "coste": coste, "terminada": terminada,
                            "orden": fila})
    with db.tx(con):
        for pj in parejas:
            nombre = pj["coste"] or pj["etiqueta"].lower()
            con.execute("INSERT INTO aud_categorias (obra_id, nombre, tipo, orden) VALUES (?,?,?,?) ON CONFLICT(obra_id, nombre) DO NOTHING",
                        (obra_id, nombre, "DIRECTO", pj["orden"]))
            con.execute("UPDATE aud_categorias SET nombre_venta=?, terminada=?, orden=? WHERE obra_id=? AND nombre=?",
                        (pj["venta"], int(pj["terminada"]), pj["orden"], obra_id, nombre))
    out["categorias_analizadas"] = len(parejas)
    out["categorias_terminadas"] = [p["etiqueta"] for p in parejas if p["terminada"]]

    # histórico mensual: fila de meses «ANÁLISIS <MES>» y filas VENTAS/COMPRAS/RRHH/corrección
    fila_meses = next((int(k[1:]) for k, v in vals.items() if isinstance(v, str) and norm(v).startswith("ANALISIS ENERO")
                       and re.match(r"^[A-Z]\d+$", k)), None)
    if fila_meses:
        from openpyxl.utils import get_column_letter, column_index_from_string
        cols = sorted({re.match(r"([A-Z]+)", k).group(1) for k, v in vals.items() if k.endswith(str(fila_meses)) and
                       re.match(rf"^[A-Z]+{fila_meses}$", k) and isinstance(v, str) and norm(v).startswith("ANALISIS")},
                      key=column_index_from_string)
        filas_et = {norm(vals.get(f"A{i}", "")): i for i in range(fila_meses, fila_meses + 15)}
        fv, fc_, fr, fco = (filas_et.get("VENTAS SIS"), filas_et.get("COMPRAS SIS"), filas_et.get("RRHH SIS"),
                            filas_et.get("CORRECCION"))
        # el último mes es el de la certificación de la hoja; se reconstruyen los años hacia atrás
        ult = datetime.now().date()
        hc_ = _hoja(wv, "CERTIFICACION")
        if hc_:
            for f in wv[hc_].iter_rows(min_row=1, max_row=15, values_only=True):
                for x in f:
                    if isinstance(x, datetime):
                        ult = x.date()
        mes_ult = MESES.index(norm(vals[f"{cols[-1]}{fila_meses}"]).replace("ANALISIS ", "")) + 1
        anio = ult.year if mes_ult <= ult.month else ult.year - 1
        meses = []
        y, mth = anio, mes_ult
        for _ in cols:
            meses.append(f"{y:04d}-{mth:02d}")
            mth -= 1
            if mth == 0:
                y, mth = y - 1, 12
        meses.reverse()
        with db.tx(con):
            for col, mes in zip(cols, meses):
                g = lambda fila: vals.get(f"{col}{fila}") if fila else None  # noqa: E731
                if isinstance(g(fv), (int, float)):
                    con.execute("INSERT INTO aud_historico (obra_id, mes, ventas, compras, rrhh, correccion, fuente) VALUES (?,?,?,?,?,?,?) "
                                "ON CONFLICT(obra_id, mes) DO UPDATE SET ventas=excluded.ventas, compras=excluded.compras, "
                                "rrhh=excluded.rrhh, correccion=excluded.correccion, fuente=excluded.fuente",
                                (obra_id, mes, str(r2(g(fv))), str(r2(g(fc_))), str(r2(g(fr))), str(r2(g(fco))), "hoja Excel"))
        out["meses_historico"] = len(meses)
        # aviso: referencias de la proyección a una columna que no es la del último mes
        for k, f in forms.items():
            if k.startswith("D") and re.search(r"\b([A-Z])(4|13)\b", f):
                colref = re.search(r"\b([A-Z])(4|13)\b", f).group(1)
                if colref != cols[-1] and colref in cols:
                    avisos.append(f"En la hoja original, la proyección ({k}: {f}) usa la columna {colref} "
                                  f"({vals.get(colref + str(fila_meses))}) y no la del último mes ({cols[-1]}). "
                                  "Aquí se calcula siempre con el último mes.")
    return out


def _importar_criterios(con, obra_id, ws, cert_id, avisos) -> dict:
    filas = list(ws.iter_rows(values_only=True))
    cab_i = next((i for i, f in enumerate(filas[:20]) if f and sum(1 for x in f[15:] if isinstance(x, str)) >= 5), None)
    if cab_i is None:
        return {}
    cab = filas[cab_i]
    cats = {j: str(x).strip() for j, x in enumerate(cab) if j >= 15 and isinstance(x, str)}
    ids_venta = {norm(r["nombre_venta"]): r["id"] for r in db.rows(con, "SELECT id, nombre_venta FROM aud_categorias WHERE obra_id=? "
                                                                       "AND nombre_venta IS NOT NULL", (obra_id,))}
    # categorías de venta sin pareja de coste: se crean con su nombre
    with db.tx(con):
        for j, c in cats.items():
            if norm(c) not in ids_venta:
                cur = con.execute("INSERT INTO aud_categorias (obra_id, nombre, nombre_venta, tipo, orden) VALUES (?,?,?,?,?) "
                                  "ON CONFLICT(obra_id, nombre) DO UPDATE SET nombre_venta=excluded.nombre_venta",
                                  (obra_id, c.lower(), c, "DIRECTO", 900 + j))
                ids_venta[norm(c)] = db.one(con, "SELECT id FROM aud_categorias WHERE obra_id=? AND nombre=?", (obra_id, c.lower()))["id"]
    # filas de partida: código en B, título en F, importe origen en O, categoría = columna con valor
    excel = []
    for f in filas[cab_i + 1:]:
        if not f or len(f) < 16:
            continue
        cod, tit, imp = f[1], f[5], f[14]
        if cod and isinstance(imp, (int, float)) and len(f) > 10 and f[10] == "OR":
            cat = next((cats[j] for j in cats if j < len(f) and isinstance(f[j], (int, float)) and f[j] != 0), None)
            excel.append({"codigo": str(cod).strip(), "titulo": str(tit or ""), "imp": r2(imp), "cat": cat})
    # emparejado con las partidas de la certificación ya importada (mismo código; desempate por importe y título)
    if cert_id is None:
        c = db.one(con, "SELECT id FROM certificaciones WHERE obra_id=? ORDER BY numero DESC LIMIT 1", (obra_id,))
        cert_id = c and c["id"]
    n_ok, n_sin = 0, 0
    if cert_id:
        app = db.rows(con, "SELECT codigo, titulo, origen_m FROM cert_lineas WHERE cert_id=? ORDER BY orden", (cert_id,))
        por_cod = defaultdict(list)
        for e in excel:
            por_cod[e["codigo"]].append(e)
        def raiz(c):
            return re.split(r"[\s-]", str(c).strip())[0].upper()
        por_raiz = defaultdict(list)
        for e in excel:
            por_raiz[raiz(e["codigo"])].append(e)
        asignado = {}
        # pasada 1: mismo código (o código con espacio) y MISMO importe origen -> emparejado inequívoco
        for i, a in enumerate(app):
            imp_a = r2(Decimal(a["origen_m"]) / 1000)
            cods = [a["codigo"]] + ([f"{a['codigo']} {a['titulo'].split()[0]}"] if a["titulo"] else [])
            for cod in cods:
                e = next((x for x in por_cod.get(cod, []) if not x.get("_usado") and x["imp"] == imp_a), None)
                if e:
                    e["_usado"] = True
                    asignado[i] = e
                    break
        # pasada 2: mismo código raíz; desempate por importe en valor absoluto y similitud de título
        for i, a in enumerate(app):
            if i in asignado:
                continue
            imp_a = r2(Decimal(a["origen_m"]) / 1000)
            cands = [x for x in por_cod.get(a["codigo"], []) + por_raiz.get(raiz(a["codigo"]), []) if not x.get("_usado")]
            if not cands:
                continue
            e = next((x for x in cands if abs(x["imp"]) == abs(imp_a)), None) or \
                max(cands, key=lambda x: fuzz.token_set_ratio(norm(x["titulo"]), norm(a["titulo"])))
            if e["imp"] == imp_a or fuzz.token_set_ratio(norm(e["titulo"]), norm(a["titulo"])) >= 70:
                e["_usado"] = True
                asignado[i] = e
        with db.tx(con):
            for i, a in enumerate(app):
                e = asignado.get(i)
                if not e:
                    n_sin += 1
                    continue
                if e["cat"]:
                    con.execute("INSERT INTO aud_criterios (obra_id, clave, codigo, titulo, categoria_id, origen) VALUES (?,?,?,?,?,?) "
                                "ON CONFLICT(obra_id, clave) DO UPDATE SET categoria_id=excluded.categoria_id, origen=excluded.origen",
                                (obra_id, clave_partida(a["codigo"], a["titulo"]), a["codigo"], a["titulo"],
                                 ids_venta.get(norm(e["cat"])), "excel"))
                    n_ok += 1
    else:
        avisos.append("Importa antes la certificación (PDF) de la obra para enlazar los criterios con sus partidas.")
    return {"criterios_excel": len(excel), "criterios_enlazados": n_ok, "partidas_sin_criterio": n_sin}


# ============================================================================ criterios sobre una certificación
def criterios_cert(con, obra_id: int, cert_id: int) -> pd.DataFrame:
    """Partidas de la certificación con su categoría; si no tienen criterio, propone uno (subcapítulo mayoritario)."""
    lin = pd.DataFrame(db.rows(con, "SELECT id, orden, capitulo, subcapitulo, codigo, unidad, titulo, origen_m, actual_m, precio "
                                    "FROM cert_lineas WHERE cert_id=? ORDER BY orden", (cert_id,)))
    if lin.empty:
        return lin
    crit = {r["clave"]: r for r in db.rows(con, "SELECT clave, categoria_id, origen FROM aud_criterios WHERE obra_id=?", (obra_id,))}
    lin["clave"] = [clave_partida(c, t) for c, t in zip(lin["codigo"], lin["titulo"])]
    lin["categoria_id"] = lin["clave"].map(lambda k: crit[k]["categoria_id"] if k in crit else None)
    lin["criterio"] = lin["clave"].map(lambda k: crit[k]["origen"] if k in crit else None)
    # sugerencia: la categoría mayoritaria (por importe) del mismo subcapítulo o, si no, del capítulo
    lin["sugerida_id"] = None
    con_cat = lin[lin["categoria_id"].notna()]
    for nivel in ("subcapitulo", "capitulo"):
        peso = con_cat.groupby([nivel, "categoria_id"])["origen_m"].apply(lambda s: s.abs().sum()).reset_index()
        mayor = peso.sort_values("origen_m").drop_duplicates(nivel, keep="last").set_index(nivel)["categoria_id"]
        faltan = lin["categoria_id"].isna() & lin["sugerida_id"].isna()
        lin.loc[faltan, "sugerida_id"] = lin.loc[faltan, nivel].map(mayor)
    lin["nueva"] = lin["categoria_id"].isna() & (lin["origen_m"] != 0)
    return lin


def guardar_criterios(con, obra_id: int, filas: list[dict], usuario: str, origen: str = "manual") -> int:
    n = 0
    with db.tx(con):
        for f in filas:
            if f.get("categoria_id") is None:
                continue
            con.execute("INSERT INTO aud_criterios (obra_id, clave, codigo, titulo, categoria_id, origen) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(obra_id, clave) DO UPDATE SET categoria_id=excluded.categoria_id, origen=excluded.origen",
                        (obra_id, clave_partida(f["codigo"], f["titulo"]), f["codigo"], f["titulo"], int(f["categoria_id"]), origen))
            n += 1
        db.audit(con, usuario, "criterios_partidas", "obra", obra_id, {"n": n, "origen": origen})
    return n


# ============================================================================ cálculo
def parametros(con, obra_id: int) -> dict:
    con.execute("INSERT OR IGNORE INTO aud_parametros (obra_id) VALUES (?)", (obra_id,))
    con.commit()
    p = db.one(con, "SELECT * FROM aud_parametros WHERE obra_id=?", (obra_id,))
    return {k: (D(v) if k not in ("obra_id", "actualizado_por", "actualizado_en") else v) for k, v in p.items()}


def categorias(con, obra_id: int) -> list[dict]:
    return db.rows(con, "SELECT * FROM aud_categorias WHERE obra_id=? ORDER BY orden, nombre", (obra_id,))


def periodo(con, cert: dict) -> tuple[str, str]:
    prev = db.one(con, "SELECT fecha FROM certificaciones WHERE obra_id=? AND numero<? ORDER BY numero DESC LIMIT 1",
                  (cert["obra_id"], cert["numero"]))
    if prev and prev["fecha"]:
        from datetime import timedelta
        return (datetime.strptime(prev["fecha"], "%Y-%m-%d").date() + timedelta(days=1)).isoformat(), cert["fecha"]
    return cert["fecha"][:8] + "01", cert["fecha"]


def calcular(con, obra_id: int, cert_id: int, corte: str | None = None, incluir_facturas_app: bool = False,
             *args, incluir_internos: bool = True, **kwargs):
    return _calcular(con, obra_id, cert_id, corte, incluir_facturas_app, *args, incluir_internos=incluir_internos, **kwargs)


def _calcular(con, obra_id: int, cert_id: int, corte: str | None = None, incluir_facturas_app: bool = False,
             modo: str = "origen", incluir_devengado: bool = False, incluir_internos: bool = True) -> dict:
    """
    modo 'origen': todo lo certificado frente a todo el coste (a la fecha de corte, o todo lo exportado).
    modo 'mes'   : lo certificado en el mes frente al coste con fecha en el periodo de esa certificación.
    """
    p = parametros(con, obra_id)
    cert = db.one(con, "SELECT * FROM certificaciones WHERE id=?", (cert_id,))
    p["ppto_estimado"] = False
    if not p["ppto_venta"]:
        # sin presupuesto introducido: venta prevista deducida de la certificación (cantidad origen ÷ % a origen)
        s_ = db.one(con, "SELECT SUM(COALESCE(presupuesto_m, origen_m)) s FROM cert_lineas WHERE cert_id=?", (cert_id,))["s"] or 0
        p["ppto_venta"], p["ppto_estimado"] = D(s_) / 1000, True
    mes_cert = cert["fecha"]
    desde = "0000-01-01"
    limite = corte or "9999-12-31"
    if modo == "mes":
        desde, limite = periodo(con, cert)
    ventas = D(cert["total_origen_m" if modo != "mes" else "total_actual_m"]) / 1000

    compras = pd.DataFrame(db.rows(con, "SELECT proveedor, proveedor_norm, base, f_contable FROM compras_sis WHERE obra_id=?", (obra_id,)))
    fuente_coste = "SIS" if not compras.empty else "facturas"
    if compras.empty:
        incluir_facturas_app = True     # sin exportación de SIS, el coste son las facturas registradas en la app
    if not compras.empty:
        compras = compras[(compras["f_contable"].isna() & (modo != "mes")) |
                          ((compras["f_contable"] >= desde) & (compras["f_contable"] <= limite))]
    extra = pd.DataFrame()
    if incluir_facturas_app:
        # facturas registradas en la app que aún no están en SIS (por nº de factura normalizado)
        en_sis = {re.sub(r"[^A-Z0-9]", "", str(x).upper()) for x in
                  [r["num_factura"] for r in db.rows(con, "SELECT num_factura FROM compras_sis WHERE obra_id=?", (obra_id,))]}
        app = pd.DataFrame(db.rows(con, """SELECT COALESCE(pr.nombre, d.emisor_nombre) AS proveedor, d.numero,
                                           d.base_imponible_cents, d.fecha FROM documentos d LEFT JOIN proveedores pr ON pr.id=d.proveedor_id
                                           WHERE d.obra_id=? AND d.estado<>'rechazada' AND d.tipo_documento IN ('factura','abono','anticipo')
                                           AND d.fecha>=? AND d.fecha<=?""", (obra_id, desde, limite)))
        if not app.empty:
            app = app[~app["numero"].fillna("").map(lambda x: re.sub(r"[^A-Z0-9]", "", x.upper())).isin(en_sis)]
            extra = pd.DataFrame({"proveedor": app["proveedor"], "proveedor_norm": app["proveedor"].map(norm),
                                  "base": app["base_imponible_cents"].map(lambda c: str(D(c or 0) / 100)), "f_contable": app["fecha"]})
            compras = pd.concat([compras, extra], ignore_index=True) if not compras.empty else extra
    from .obra_control import devengado_pendiente
    dev = devengado_pendiente(con, obra_id, limite if limite != "9999-12-31" else None)
    if modo == "mes":
        dev = [x for x in dev if not x["fecha"] or x["fecha"] >= desde]
    total_devengado = sum((D(x["base_c"]) / 100 for x in dev), Z)
    if incluir_devengado and dev:
        extra_dev = pd.DataFrame({"proveedor": [x["proveedor"] for x in dev], "proveedor_norm": [norm(x["proveedor"]) for x in dev],
                                  "base": [str(D(x["base_c"]) / 100) for x in dev], "f_contable": [x["fecha"] for x in dev]})
        compras = pd.concat([compras, extra_dev], ignore_index=True) if not compras.empty else extra_dev
    total_compras = sum((D(x) for x in compras["base"]), Z) if not compras.empty else Z

    rrhh_rows = db.rows(con, "SELECT coste, categoria, fecha FROM rrhh_sis WHERE obra_id=? AND fecha>=? AND fecha<=?",
                        (obra_id, desde, limite))
    total_rrhh = sum((D(r["coste"]) for r in rrhh_rows), Z)
    rrhh_ind = sum((D(r["coste"]) for r in rrhh_rows if norm(r["categoria"]).startswith("INDIRECTO")), Z)

    # costes y ventas internos (mano de obra propia, indirectos, gastos generales, cargos a subcontratas, venta sin certificar)
    internos_r = None
    if incluir_internos:
        try:
            from . import internos as _int
            _int.init(con)
            internos_r = _int.resumen(con, obra_id, desde if desde != "0000-01-01" else None,
                                      limite if limite != "9999-12-31" else None)
            total_rrhh += internos_r["mano_obra"]
            total_compras += internos_r["coste"] - internos_r["cargos"]
            ventas += internos_r["venta"]
        except Exception:
            internos_r = None
    res_sis = ventas - total_compras - total_rrhh

    # ---- por categoría
    cats = categorias(con, obra_id)
    lin = criterios_cert(con, obra_id, cert_id)
    cobrado = Counter()
    if not lin.empty:
        col = "actual_m" if modo == "mes" else "origen_m"
        for cid, m in lin[lin["categoria_id"].notna()].groupby("categoria_id")[col].sum().items():
            cobrado[int(cid)] = D(int(m)) / 1000
    pc = {r["proveedor_norm"]: r["categoria_id"] for r in db.rows(con, "SELECT proveedor_norm, categoria_id FROM aud_proveedor_cat WHERE obra_id=?",
                                                                    (obra_id,))}
    pagado = defaultdict(lambda: Z)
    sin_cat = defaultdict(lambda: Z)
    if not compras.empty:
        for r in compras.itertuples():
            cid = pc.get(r.proveedor_norm)
            if cid:
                pagado[int(cid)] += D(r.base)
            else:
                sin_cat[r.proveedor] += D(r.base)
    if internos_r:
        for cid_, v_ in internos_r["por_oficio_coste"].items():
            pagado[int(cid_)] += v_
        for cid_, v_ in internos_r["por_oficio_venta"].items():
            cobrado[int(cid_)] = cobrado.get(int(cid_), Z) + v_
    pase = p["pase"]
    filas = []
    for c in cats:
        cob, pag = cobrado.get(c["id"], Z), pagado.get(c["id"], Z)
        if not cob and not pag and not c["nombre_venta"]:
            continue
        sp = cob / (1 + pase)
        dif = sp - pag
        corrige = bool(c["nombre_venta"]) and c["tipo"] == "DIRECTO" and not c["terminada"]
        filas.append({"id": c["id"], "categoria": c["nombre"], "venta": c["nombre_venta"], "tipo": c["tipo"],
                      "terminada": bool(c["terminada"]), "cobrado": cob, "sin_pase": sp, "pagado": pag, "diferencial": dif,
                      "correccion": -dif if corrige else Z, "margen_pct": ((cob - pag) / cob * 100) if cob else None})
    correccion = sum((f["correccion"] for f in filas), Z)
    resultado = res_sis + correccion
    ventas_clasif = sum(cobrado.values(), Z)
    compras_clasif = sum(pagado.values(), Z)
    return {"cert": cert, "corte": corte or mes_cert, "corte_aplicado": bool(corte), "modo": modo,
            "periodo": (desde, limite) if modo == "mes" else (None, corte), "parametros": p, "ventas": ventas, "compras": total_compras, "rrhh": total_rrhh,
            "rrhh_indirecto": rrhh_ind, "resultado_sis": res_sis, "pct_sis": (res_sis / ventas * 100) if ventas else Z,
            "correccion": correccion, "resultado": resultado, "pct": (resultado / ventas * 100) if ventas else Z,
            "categorias": filas, "ventas_clasificadas": ventas_clasif, "compras_clasificadas": compras_clasif,
            "pct_ventas_clasif": (ventas_clasif / ventas * 100) if ventas else Z,
            "pct_compras_clasif": (compras_clasif / total_compras * 100) if total_compras else Z,
            "proveedores_sin_categoria": dict(sorted(sin_cat.items(), key=lambda x: -abs(x[1]))),
            "partidas_sin_criterio": int(lin["nueva"].sum()) if not lin.empty else 0,
            "facturas_app_sin_sis": len(extra), "n_compras": len(compras), "fuente_coste": fuente_coste,
            "devengado": total_devengado, "devengado_docs": dev, "devengado_incluido": incluir_devengado,
            "internos": internos_r}


def cobertura_coste(con, obra_id: int, r: dict) -> tuple[float, str]:
    """¿Cuánto periodo de la obra cubre el coste cargado? Evita márgenes a origen engañosos."""
    if r["fuente_coste"] == "SIS":
        return 1.0, "Coste a origen de SIS"
    fx = db.one(con, "SELECT MIN(fecha) mi, MAX(fecha) ma FROM documentos WHERE obra_id=? AND estado<>'rechazada' AND fecha IS NOT NULL",
                (obra_id,))
    if not fx or not fx["mi"]:
        return 0.0, "Sin facturas"
    meses = (datetime.strptime(fx["ma"], "%Y-%m-%d") - datetime.strptime(fx["mi"], "%Y-%m-%d")).days / 30.4 + 1
    return min(1.0, meses / max(1, r["cert"]["numero"])), f"Facturas registradas del {fx['mi']} al {fx['ma']}"


def proyeccion(r: dict) -> dict:
    p = r["parametros"]
    ppto = p["ppto_venta"]
    gg_pct = (p["gg"] / ppto) if ppto else Z
    bfo_ind = p["ind_pendiente"] - p["ind_estimado"]
    desv = -p["desv_pct"] / 100 * ppto
    fact_pend = max(Z, ppto - r["ventas"]) if r.get("modo", "origen") == "origen" else Z
    gg_pend = fact_pend * gg_pct
    total = r["resultado"] + bfo_ind + desv + gg_pend
    return {"resultado_hoy": r["resultado"], "bfo_indirectos": bfo_ind, "desviaciones": desv, "facturacion_pendiente": fact_pend,
            "gg_pct": gg_pct * 100, "gg_pendientes": gg_pend, "proyeccion": total, "pct": (total / ppto * 100) if ppto else Z}


def historico(con, obra_id: int, actual: dict | None = None) -> pd.DataFrame:
    h = pd.DataFrame(db.rows(con, "SELECT * FROM aud_historico WHERE obra_id=? ORDER BY mes", (obra_id,)))
    filas = []
    for r in h.to_dict("records") if not h.empty else []:
        v, c, rr, co = D(r["ventas"]), D(r["compras"]), D(r["rrhh"]), D(r["correccion"])
        filas.append({"mes": r["mes"], "ventas": v, "compras": c, "rrhh": rr, "resultado_sis": v - c - rr, "correccion": co,
                      "resultado": v - c - rr + co, "fuente": r["fuente"]})
    if actual:
        mes = actual["corte"][:7]
        filas = [f for f in filas if f["mes"] != mes] + [{
            "mes": mes, "ventas": actual["ventas"], "compras": actual["compras"], "rrhh": actual["rrhh"],
            "resultado_sis": actual["resultado_sis"], "correccion": actual["correccion"], "resultado": actual["resultado"],
            "fuente": "calculado"}]
    df = pd.DataFrame(sorted(filas, key=lambda f: f["mes"]))
    if not df.empty:
        df["pct_sis"] = [float(a / b * 100) if b else None for a, b in zip(df["resultado_sis"], df["ventas"])]
        df["pct"] = [float(a / b * 100) if b else None for a, b in zip(df["resultado"], df["ventas"])]
    return df


def guardar_mes(con, obra_id: int, r: dict, usuario: str):
    """Congela el cálculo del mes en el histórico (como hacía la hoja, columna a columna)."""
    mes = r["corte"][:7]
    with db.tx(con):
        con.execute("INSERT INTO aud_historico (obra_id, mes, ventas, compras, rrhh, correccion, fuente) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(obra_id, mes) DO UPDATE SET ventas=excluded.ventas, compras=excluded.compras, rrhh=excluded.rrhh, "
                    "correccion=excluded.correccion, fuente=excluded.fuente",
                    (obra_id, mes, str(r2(r["ventas"])), str(r2(r["compras"])), str(r2(r["rrhh"])), str(r2(r["correccion"])),
                     f"cerrado por {usuario}"))
        db.audit(con, usuario, "cerrar_mes_auditoria", "obra", obra_id, {"mes": mes, "resultado": str(r2(r["resultado"]))})


# ============================================================================ exportación a Excel con fórmulas
def exportar_excel(r: dict, hist: pd.DataFrame, pr: dict, obra: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    F = "Arial"
    tit = Font(name=F, bold=True, size=14, color="2E2E2E")
    cab = Font(name=F, bold=True, color="FFFFFF")
    relleno = PatternFill("solid", fgColor="2E2E2E")
    rojo = PatternFill("solid", fgColor="E1251B")
    entrada = Font(name=F, color="1F4E9A")
    normal = Font(name=F)
    negrita = Font(name=F, bold=True)
    eur = '#,##0.00 "€";[Red]-#,##0.00 "€"'
    pct = '0.00%'
    fino = Border(bottom=Side(style="thin", color="D9D7CF"))

    # --------------------------------------------------------------- Categorías
    wc = wb.active
    wc.title = "Categorías"
    wc["A1"], wc["A1"].font = f"Auditoría por categorías · {obra['codigo']} {obra['nombre']} · cert. nº {r['cert']['numero']}", tit
    wc["A2"], wc["B2"] = "PASE (GG + BI incluido en la venta)", float(r["parametros"]["pase"])
    wc["A2"].font, wc["B2"].font, wc["B2"].number_format = negrita, entrada, pct
    wc["D2"] = "Celdas en azul: datos de entrada. El resto se calcula con fórmulas."
    wc["D2"].font = Font(name=F, italic=True, color="77756E")
    heads = ["Categoría", "Tipo", "Terminada", "Cobrado a origen", "Sin pase", "Pagado", "Diferencial", "Corrección", "Margen s/ cobrado"]
    for j, h in enumerate(heads, 1):
        c = wc.cell(row=4, column=j, value=h)
        c.font, c.fill, c.alignment = cab, relleno, Alignment(horizontal="center", wrap_text=True)
    i0 = 5
    for i, f in enumerate(r["categorias"], i0):
        wc.cell(row=i, column=1, value=f["categoria"]).font = normal
        wc.cell(row=i, column=2, value=f["tipo"]).font = normal
        wc.cell(row=i, column=3, value="SI" if f["terminada"] else "NO").font = entrada
        wc.cell(row=i, column=4, value=float(f["cobrado"])).font = entrada
        wc.cell(row=i, column=5, value=f"=D{i}/(1+$B$2)")
        wc.cell(row=i, column=6, value=float(f["pagado"])).font = entrada
        wc.cell(row=i, column=7, value=f"=E{i}-F{i}")
        wc.cell(row=i, column=8, value=f'=IF(AND(B{i}="DIRECTO",C{i}="NO",D{i}<>0),-G{i},0)')
        wc.cell(row=i, column=9, value=f"=IF(D{i}=0,\"\",(D{i}-F{i})/D{i})")
        for j in range(4, 9):
            wc.cell(row=i, column=j).number_format = eur
        wc.cell(row=i, column=9).number_format = pct
        for j in range(1, 10):
            wc.cell(row=i, column=j).border = fino
    fin = i0 + len(r["categorias"]) - 1
    t = fin + 1
    wc.cell(row=t, column=1, value="TOTAL").font = negrita
    for j in range(4, 9):
        L = get_column_letter(j)
        c = wc.cell(row=t, column=j, value=f"=SUM({L}{i0}:{L}{fin})")
        c.font, c.number_format = negrita, eur
    for j, w in enumerate([34, 12, 11, 18, 18, 18, 18, 18, 14], 1):
        wc.column_dimensions[get_column_letter(j)].width = w
    wc.freeze_panes = "A5"

    # --------------------------------------------------------------- Auditoría del mes
    wa = wb.create_sheet("Auditoría", 0)
    wa["A1"], wa["A1"].font = f"Auditoría mensual · {obra['codigo']} {obra['nombre']}", tit
    wa["A2"] = f"Certificación nº {r['cert']['numero']} del {r['cert']['fecha']} · corte de costes {r['corte']}"
    wa["A2"].font = Font(name=F, color="77756E")
    filas = [("VENTAS SIS (certificado a origen)", float(r["ventas"]), True),
             ("COMPRAS SIS", float(r2(r["compras"])), True),
             ("RRHH SIS", float(r2(r["rrhh"])), True),
             ("RESULTADO SIS", "=B4-B5-B6", False),
             ("% sobre ventas", "=IF(B4=0,0,B7/B4)", False),
             ("Corrección por categorías en curso", f"='Categorías'!H{t}", False),
             ("RESULTADO", "=B7+B9", False),
             ("% sobre ventas", "=IF(B4=0,0,B10/B4)", False)]
    for i, (et, v, es_in) in enumerate(filas, 4):
        wa.cell(row=i, column=1, value=et).font = negrita if et.startswith("RESULTADO") else normal
        c = wa.cell(row=i, column=2, value=v)
        c.font = entrada if es_in else (negrita if et.startswith("RESULTADO") else normal)
        c.number_format = pct if et.startswith("%") else eur
        wa.cell(row=i, column=1).border = wa.cell(row=i, column=2).border = fino
    for c in (wa["A10"], wa["B10"]):
        c.fill, c.font = rojo, Font(name=F, bold=True, color="FFFFFF")

    wa["A13"], wa["A13"].font = "PROYECCIÓN A FIN DE OBRA", tit
    pp = r["parametros"]
    proy = [("Presupuesto total de venta", float(r2(pp["ppto_venta"])), True, eur),
            ("Gastos generales (GG) presupuestados", float(r2(pp["gg"])), True, eur),
            ("% GG", "=IF(B14=0,0,B15/B14)", False, pct),
            ("Indirectos: presupuesto pendiente de consumir", float(r2(pp["ind_pendiente"])), True, eur),
            ("Indirectos: coste estimado hasta fin", float(r2(pp["ind_estimado"])), True, eur),
            ("Desviación de coste prevista (% s/ presupuesto)", float(pp["desv_pct"] / 100), True, pct),
            ("RESULTADO HOY", "=B10", False, eur),
            ("Beneficio por indirectos", "=B17-B18", False, eur),
            ("Desviaciones de coste", "=-B19*B14", False, eur),
            ("Facturación pendiente", "=B14-B4", False, eur),
            ("GG pendientes", "=B23*B16", False, eur),
            ("PROYECCIÓN DE RESULTADO", "=B20+B21+B22+B24", False, eur),
            ("% resultado de obra", "=IF(B14=0,0,B25/B14)", False, pct)]
    for i, (et, v, es_in, fmt) in enumerate(proy, 14):
        wa.cell(row=i, column=1, value=et).font = negrita if et.isupper() else normal
        c = wa.cell(row=i, column=2, value=v)
        c.font, c.number_format = (entrada if es_in else (negrita if et.isupper() else normal)), fmt
        wa.cell(row=i, column=1).border = wa.cell(row=i, column=2).border = fino
    for c in (wa["A25"], wa["B25"]):
        c.fill, c.font = rojo, Font(name=F, bold=True, color="FFFFFF")
    wa.column_dimensions["A"].width, wa.column_dimensions["B"].width = 48, 22
    wa["D4"] = "Celdas en azul: datos de entrada (de SIS, la certificación o parámetros). El resto son fórmulas."
    wa["D4"].font = Font(name=F, italic=True, color="77756E")

    # --------------------------------------------------------------- Evolución
    if not hist.empty:
        we = wb.create_sheet("Evolución")
        we["A1"], we["A1"].font = "Evolución mensual a origen", tit
        hs = ["Mes", "Ventas SIS", "Compras SIS", "RRHH SIS", "Resultado SIS", "% SIS", "Corrección", "Resultado", "%", "Fuente"]
        for j, h in enumerate(hs, 1):
            c = we.cell(row=3, column=j, value=h)
            c.font, c.fill = cab, relleno
        for i, row in enumerate(hist.to_dict("records"), 4):
            we.cell(row=i, column=1, value=row["mes"])
            for j, k in ((2, "ventas"), (3, "compras"), (4, "rrhh"), (7, "correccion")):
                c = we.cell(row=i, column=j, value=float(r2(row[k])))
                c.font, c.number_format = entrada, eur
            we.cell(row=i, column=5, value=f"=B{i}-C{i}-D{i}").number_format = eur
            we.cell(row=i, column=6, value=f"=IF(B{i}=0,0,E{i}/B{i})").number_format = pct
            we.cell(row=i, column=8, value=f"=E{i}+G{i}").number_format = eur
            we.cell(row=i, column=9, value=f"=IF(B{i}=0,0,H{i}/B{i})").number_format = pct
            we.cell(row=i, column=10, value=row["fuente"])
        for j, w in enumerate([10, 18, 18, 16, 18, 9, 18, 18, 9, 22], 1):
            we.column_dimensions[get_column_letter(j)].width = w
    for ws in wb.worksheets:
        for fila in ws.iter_rows():
            for c in fila:
                if c.font and c.font.name != F:
                    c.font = Font(name=F, bold=c.font.bold, italic=c.font.italic, color=c.font.color, size=c.font.size)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
