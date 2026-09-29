"""
Maestros: obras, partidas y proveedores.

- Identificación de obra: 1) código numérico literal en la factura (p.ej. "664"),
  2) coincidencia de alias, 3) similitud difusa (rapidfuzz) con umbral.
- Clasificación de líneas en partidas: la IA propone; si no hay IA o no está
  segura, un clasificador determinista por palabras clave (con puntuación).
"""
from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from . import db
from .fiscal import normalize_nif, normalize_iban
from .money import to_cents

# Capítulos estándar de edificación (se crean en cada obra nueva; editables)
PARTIDAS_ESTANDAR = [
    ("01", "Movimiento de tierras y demoliciones", "excavacion, demolicion, tierras, zanja, relleno, desbroce, vaciado"),
    ("02", "Cimentación y estructura", "hormigon, ferralla, encofrado, forjado, gunita, gunitado, acero corrugado, pilar, viga, losa, zapata, hormigonado, bombeo"),
    ("03", "Albañilería y cerramientos", "ladrillo, bloque, tabique, tabiqueria, mortero, cemento, albanil, albanileria, arena, cerramiento, recrecido, maestreado"),
    ("04", "Cubiertas e impermeabilización", "cubierta, impermeabilizacion, tela asfaltica, lamina, teja, aislamiento"),
    ("05", "Revestimientos, yesos y falsos techos", "yeso, enlucido, pladur, placa de yeso, falso techo, techos, guarnecido, repaso, repasos, escayola, perfileria"),
    ("06", "Solados y alicatados", "gres, porcelanico, solado, rodapie, alicatado, alicatar, pavimento, azulejo, ceramica, baldosa, pav, rev, antid, lakestone, junta, cemento cola, adhesivo cementoso"),
    ("07", "Carpintería exterior, vidrios y cerramientos ligeros", "aluminio, ventana, acristalamiento, vidrio, composite, voladizo, carpinteria exterior, dintel, persiana, mosquitera"),
    ("08", "Carpintería interior y cerrajería", "puerta, cerradura, electrocerradura, cerrajeria, barandilla, armario, carpinteria interior, cortafuegos, registro, herraje"),
    ("09", "Fontanería, saneamiento y aparatos sanitarios", "tuberia, ppr, pvc, inodoro, grifo, griferia, desague, colector, sifon, sifonico, fontaneria, saneamiento, lavabo, fregadero, latiguillo, mangueton, bajante"),
    ("10", "Electricidad e iluminación", "cable, cableado, cuadro electrico, luminaria, mecanismo, enchufe, iluminacion, electricidad, interruptor, led"),
    ("11", "Climatización y ventilación", "climatizacion, mitsubishi, recuperador, conducto, split, aerotermia, nitrogeno, refrigerante, frigorifica, ventilacion, extraccion, cobre, unidad interior, unidad exterior"),
    ("12", "Pintura y papel pintado", "pintura, pintado, papel pintado, papel, vinilo, vinyl, esmalte, plastica, imprimacion, revestimiento vinilico"),
    ("13", "Telecomunicaciones, seguridad y control", "cra, alarma, conexion a cra, gps, sim, telecomunicaciones, videoportero, antena, control de accesos"),
    ("14", "Mano de obra por administración", "horas, hora, peon, oficial, administracion, jornada, mano de obra"),
    ("15", "Medios auxiliares y gastos de obra", "limpieza, alquiler, andamio, contenedor, wc quimico, grua, caseta, portes, transporte, maquinaria, herramienta, papel higienico"),
    ("16", "Gestión de residuos", "residuo, residuos, escombro, rcd, vertedero, gestor"),
    ("17", "Seguridad y salud", "seguridad y salud, epi, epis, casco, arnes, proteccion colectiva, señalizacion"),
]
PARTIDA_SIN_ASIGNAR = ("99", "Sin asignar", "")


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# --------------------------------------------------------------------------- obras
def seed_inicial(con) -> None:
    """Crea la obra 664 (detectada en el lote de facturas) si la BD está vacía."""
    if db.one(con, "SELECT id FROM obras LIMIT 1"):
        return
    crear_obra(
        con, "664", "Alibuilding Benidorm", cliente="Alibuilding",
        direccion="Benidorm (Alicante)",
        alias="ALIBUILDING, ALIBULDING, ALIBUIDING, ALIBULDING BENIDORM, APTOS BENIDORM BEACH, "
              "APTOS BB, BENIDORM BEACH, 40V RESID ALIBUILDING, HOTEL ALIBUILDING, R.4819",
        usuario="sistema",
    )


def crear_obra(con, codigo, nombre, cliente="", direccion="", alias="", presupuesto_venta=0,
               presupuesto_coste=0, usuario="", con_partidas=True) -> int:
    with db.tx(con):
        cur = con.execute(
            "INSERT INTO obras (codigo, nombre, cliente, direccion, alias, presupuesto_venta_cents, presupuesto_coste_cents) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(codigo).strip(), nombre.strip(), cliente, direccion, alias,
             to_cents(presupuesto_venta), to_cents(presupuesto_coste)),
        )
        obra_id = cur.lastrowid
        if con_partidas:
            for cod, desc, kw in PARTIDAS_ESTANDAR + [PARTIDA_SIN_ASIGNAR]:
                con.execute("INSERT INTO partidas (obra_id, codigo, descripcion, palabras_clave) VALUES (?,?,?,?)",
                            (obra_id, cod, desc, kw))
        db.audit(con, usuario, "crear_obra", "obra", obra_id, {"codigo": codigo, "nombre": nombre})
    return obra_id


def listar_obras(con, solo_activas=True) -> list[dict]:
    sql = "SELECT * FROM obras" + (" WHERE activa=1" if solo_activas else "") + " ORDER BY codigo"
    return db.rows(con, sql)


def partidas_de_obra(con, obra_id: int) -> list[dict]:
    return db.rows(con, "SELECT * FROM partidas WHERE obra_id=? ORDER BY codigo", (obra_id,))


def partida_sin_asignar(con, obra_id: int) -> int | None:
    r = db.one(con, "SELECT id FROM partidas WHERE obra_id=? AND codigo='99'", (obra_id,))
    if r:
        return r["id"]
    cur = con.execute("INSERT INTO partidas (obra_id, codigo, descripcion) VALUES (?,?,?)",
                      (obra_id, "99", "Sin asignar"))
    con.commit()
    return cur.lastrowid


def identificar_obra(con, *textos: str) -> tuple[int | None, float, str]:
    """
    Devuelve (obra_id, confianza 0..1, motivo).
    Estrategia en cascada, de más a menos fiable.
    """
    obras = listar_obras(con, solo_activas=False)
    if not obras:
        return None, 0.0, "No hay obras dadas de alta"
    texto = " | ".join(t for t in textos if t)
    tnorm = strip_accents(texto)
    if not tnorm.strip():
        return None, 0.0, "La factura no contiene referencia de obra"

    # 1) Código literal como palabra completa ("OBRA 664", "REF 664", "664 Alibuilding")
    for o in obras:
        cod = re.escape(strip_accents(o["codigo"]))
        if re.search(rf"(?<![\d.,/]){cod}(?![\d.,/])", tnorm):
            # Refuerzo si además aparece el nombre o un alias
            nombres = [strip_accents(o["nombre"])] + [strip_accents(a) for a in (o["alias"] or "").split(",") if a.strip()]
            refuerzo = any(n.split()[0] in tnorm for n in nombres if n.split())
            return o["id"], 0.99 if refuerzo else 0.9, f"Código de obra «{o['codigo']}» encontrado en el documento"

    # 2) Alias / nombre exactos
    for o in obras:
        candidatos = [o["nombre"]] + [a for a in (o["alias"] or "").split(",")]
        for c in candidatos:
            c = strip_accents(c.strip())
            if len(c) >= 4 and c in tnorm:
                return o["id"], 0.85, f"Alias «{c}» encontrado en el documento"

    # 3) Similitud difusa (tolerante a erratas: ALIBULDING / ALIBUIDING)
    mejor, mejor_score = None, 0.0
    for o in obras:
        candidatos = [o["nombre"]] + [a for a in (o["alias"] or "").split(",") if a.strip()]
        for c in candidatos:
            s = fuzz.partial_ratio(strip_accents(c.strip()), tnorm) / 100
            if s > mejor_score:
                mejor, mejor_score = o, s
    if mejor and mejor_score >= 0.85:
        return mejor["id"], round(mejor_score * 0.8, 2), f"Similitud {mejor_score:.0%} con «{mejor['nombre']}»"
    return None, 0.0, "No se ha podido identificar la obra con seguridad"


# --------------------------------------------------------------------------- partidas
# Vocabulario de materiales y servicios de obra por capítulo estándar (se suma a las palabras clave editables)
VOCABULARIO_EXTRA = {
    "movimiento de tierras y demoliciones": "machaca grava zahorra arena aridos arido big bag relleno",
    "cimentacion y estructura": "hormisaco hormigon corrugado mallazo malla electrosoldada ferralla encofrado acero b500 separador",
    "albanileria y cerramientos": "mortero ladrillo panal bloque tabicon cemento yeso escayola dintel viga dintel arena rasillon cal mapegrout reparacion autonivel nivelante perlita",
    "cubiertas e impermeabilizacion": "impermeable impermeabilizante mapelastic lamina water stop kerdi bardo tela asfaltica sellador fondo junta",
    "solados y alicatados": "azulejo porcelanico gres ess ceramico cemento cola mortar flexible rodapie junta",
    "fontaneria saneamiento y aparatos sanitarios": "tapa alcantarilla arqueta sumidero tubo pvc tuberia desague registro",
    "medios auxiliares y gastos de obra": "palet porte portes plastico discos corte herramienta alquiler contenedor limpieza",
    "pintura y papel pintado": "pintura plastica esmalte imprimacion rodillo",
    "electricidad e iluminacion": "cable caja mecanismo tubo corrugado luminaria",
}


def _kw_partida(p: dict) -> list[str]:
    kws = [strip_accents(k.strip()) for k in (p.get("palabras_clave") or "").split(",") if k.strip()]
    kws += [strip_accents(p["descripcion"])]
    extra = VOCABULARIO_EXTRA.get(strip_accents(p["descripcion"]).replace(",", ""), "")
    if not extra:
        for nombre, v in VOCABULARIO_EXTRA.items():
            if fuzz.token_set_ratio(nombre, strip_accents(p["descripcion"])) >= 80:
                extra = v
                break
    kws += extra.split()
    return list(dict.fromkeys(k for k in kws if k))     # sin repetidos: una palabra cuenta una vez


def clasificar_linea(descripcion: str, partidas: list[dict]) -> tuple[int | None, float]:
    """Clasificador determinista por palabras clave (con raíces: «mortero» encuentra «morteros»).
    Devuelve (partida_id, confianza)."""
    d = " " + re.sub(r"[^a-z0-9ñ ]", " ", strip_accents(descripcion)) + " "
    mejor, mejor_score, segundo, mejor_pos = None, 0, 0, 10 ** 6
    for p in partidas:
        if p["codigo"] == "99":
            continue
        pos = 10 ** 6
        score = 0
        for k in _kw_partida(p):
            m_ = re.search(rf"\b{re.escape(k if (' ' in k or len(k) < 5) else k[:max(4, len(k) - 2)])}", d)
            if m_:
                pos = min(pos, m_.start())
            if " " in k:
                if re.search(rf"\b{re.escape(k)}\b", d):
                    score += 2                                  # frases pesan más que palabras sueltas
            elif len(k) >= 5:
                if re.search(rf"\b{re.escape(k)}\b", d):
                    score += 1.5                                # palabra exacta
                elif re.search(rf"\b{re.escape(k[:max(4, len(k) - 2)])}\w*\b", d):
                    score += 1                                  # raíz: mortero/morteros, impermeable/impermeabilizante
            elif re.search(rf"\b{re.escape(k)}\b", d):
                score += 1
        # a igualdad de puntos manda la palabra que aparece antes (el producto suele ir primero: «PANAL HORMIGÓN…»)
        if score > mejor_score or (score == mejor_score and score > 0 and pos < mejor_pos):
            segundo, mejor, mejor_score, mejor_pos = max(segundo, mejor_score if score > mejor_score else segundo), p, score, pos
        elif score > segundo:
            segundo = score
    if not mejor or mejor_score == 0:
        return None, 0.0
    margen = mejor_score - segundo
    conf = min(0.8, 0.4 + 0.15 * mejor_score + 0.1 * margen)
    return mejor["id"], round(conf, 2)


def partida_por_codigo(partidas: list[dict], codigo: str | None) -> int | None:
    if not codigo:
        return None
    c = str(codigo).strip()
    for p in partidas:
        if p["codigo"] == c:
            return p["id"]
    return None


# --------------------------------------------------------------------------- proveedores
def upsert_proveedor(con, nif: str | None, nombre: str | None) -> int | None:
    n = normalize_nif(nif)
    nombre = (nombre or "").strip() or (n or "Proveedor sin identificar")
    if n:
        r = db.one(con, "SELECT id FROM proveedores WHERE nif=?", (n,))
        if r:
            return r["id"]
        cur = con.execute("INSERT INTO proveedores (nif, nombre) VALUES (?,?)", (n, nombre))
        return cur.lastrowid
    # Sin NIF: se intenta casar por nombre (similitud alta)
    mejor, score = None, 0
    for p in db.rows(con, "SELECT id, nombre FROM proveedores"):
        s = fuzz.token_sort_ratio(strip_accents(p["nombre"]), strip_accents(nombre))
        if s > score:
            mejor, score = p, s
    if mejor and score >= 92:
        return mejor["id"]
    cur = con.execute("INSERT INTO proveedores (nif, nombre) VALUES (NULL, ?)", (nombre,))
    return cur.lastrowid


def registrar_iban(con, proveedor_id: int, iban: str, fecha: str) -> dict:
    """
    Registra el IBAN usado por el proveedor y devuelve el contexto de riesgo:
    {'nuevo': bool, 'ibans_previos': [...]}. Un IBAN nuevo en un proveedor con
    historial es la señal clásica de fraude por suplantación de proveedor.
    """
    iban = normalize_iban(iban)
    if not iban or not proveedor_id:
        return {"nuevo": False, "ibans_previos": []}
    previos = [r["iban"] for r in db.rows(con, "SELECT iban FROM proveedor_ibans WHERE proveedor_id=?", (proveedor_id,))]
    if iban in previos:
        con.execute("UPDATE proveedor_ibans SET veces=veces+1, ultima_vez=MAX(COALESCE(ultima_vez,''),?) "
                    "WHERE proveedor_id=? AND iban=?", (fecha or "", proveedor_id, iban))
        return {"nuevo": False, "ibans_previos": previos}
    con.execute("INSERT INTO proveedor_ibans (proveedor_id, iban, primera_vez, ultima_vez) VALUES (?,?,?,?)",
                (proveedor_id, iban, fecha, fecha))
    return {"nuevo": True, "ibans_previos": previos}
