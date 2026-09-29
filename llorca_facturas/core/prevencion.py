"""
SEGURIDAD Y SALUD: coordinación de actividades empresariales (CAE), control de acceso y trazabilidad de residuos.

CAE (RD 171/2004, Ley 32/2006 de subcontratación)
- Empresas en cada obra con su alta y baja, nivel de subcontratación y a quién subcontrata.
- Documentación exigida por empresa (seguro de RC, REA, certificados de estar al corriente con AEAT y Seguridad Social,
  RNT/TC2, evaluación de riesgos y adhesión al plan de seguridad…) y por trabajador (formación PRL del convenio, aptitud
  médica, entrega de EPI, alta en la Seguridad Social). Cada documento con su caducidad y su validación.
- Catálogo configurable: qué se exige, a quién y cada cuánto caduca.

Control de acceso
- `puede_acceder(obra, trabajador, fecha)`: verde o rojo con los motivos (empresa sin alta en la obra, documento que
  falta o caducado, trabajador de baja). Cada consulta en obra queda registrada (entrada permitida o denegada).
- Opcional: aviso en pagos si el certificado de estar al corriente con Hacienda del subcontratista está caducado
  (responsabilidad subsidiaria del art. 43.1.f LGT).

Residuos (RD 105/2008 y Ley 7/2022)
- Cada retirada: código LER, cantidad, transportista, gestor autorizado, albarán (nº y archivo), factura (enlazada a la
  factura del circuito, propuesta automáticamente por el nº de albarán) y certificado del gestor.
- Trazabilidad completa = albarán + factura + certificado. Se avisa de las retiradas sin certificado pasados N días.
- Resumen por código LER frente a lo previsto en el estudio de gestión de residuos.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from . import db
from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS cae_requisitos (
    id INTEGER PRIMARY KEY, ambito TEXT NOT NULL, nombre TEXT NOT NULL, meses_validez INTEGER, obligatorio INTEGER DEFAULT 1,
    bloquea_acceso INTEGER DEFAULT 1, activo INTEGER DEFAULT 1, UNIQUE (ambito, nombre));
CREATE TABLE IF NOT EXISTS cae_empresas_obra (
    id INTEGER PRIMARY KEY, obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    proveedor_id INTEGER NOT NULL REFERENCES proveedores(id), fecha_alta TEXT NOT NULL, fecha_baja TEXT,
    nivel INTEGER DEFAULT 1, contratista_id INTEGER, actividad TEXT, contacto TEXT, email TEXT, usuario TEXT, creado_en TEXT);
CREATE TABLE IF NOT EXISTS cae_trabajadores (
    id INTEGER PRIMARY KEY, proveedor_id INTEGER NOT NULL REFERENCES proveedores(id), nombre TEXT NOT NULL, dni TEXT NOT NULL,
    puesto TEXT, activo INTEGER DEFAULT 1, creado_en TEXT, UNIQUE (dni));
CREATE TABLE IF NOT EXISTS cae_trabajador_obra (
    id INTEGER PRIMARY KEY, trabajador_id INTEGER NOT NULL REFERENCES cae_trabajadores(id) ON DELETE CASCADE,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE, fecha_alta TEXT NOT NULL, fecha_baja TEXT, UNIQUE (trabajador_id, obra_id, fecha_alta));
CREATE TABLE IF NOT EXISTS cae_documentos (
    id INTEGER PRIMARY KEY, requisito_id INTEGER NOT NULL REFERENCES cae_requisitos(id),
    proveedor_id INTEGER, trabajador_id INTEGER, fecha_emision TEXT, fecha_caducidad TEXT, archivo TEXT, sha256 TEXT,
    estado TEXT DEFAULT 'pendiente_validar', validado_por TEXT, validado_en TEXT, motivo_rechazo TEXT, subido_por TEXT, subido_en TEXT);
CREATE INDEX IF NOT EXISTS ix_caedoc ON cae_documentos(requisito_id, proveedor_id, trabajador_id);
CREATE TABLE IF NOT EXISTS cae_accesos (
    id INTEGER PRIMARY KEY, obra_id INTEGER, trabajador_id INTEGER, dni TEXT, fecha_hora TEXT, permitido INTEGER, motivos TEXT, usuario TEXT);
CREATE TABLE IF NOT EXISTS residuos (
    id INTEGER PRIMARY KEY, obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE, fecha TEXT NOT NULL,
    ler TEXT NOT NULL, descripcion TEXT, cantidad TEXT, unidad TEXT DEFAULT 't', transportista TEXT, gestor TEXT, gestor_nima TEXT,
    albaran TEXT, albaran_archivo TEXT, documento_id INTEGER, certificado TEXT, certificado_archivo TEXT, certificado_fecha TEXT,
    notas TEXT, usuario TEXT, creado_en TEXT);
CREATE TABLE IF NOT EXISTS residuos_previstos (
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE, ler TEXT NOT NULL, descripcion TEXT, cantidad TEXT, unidad TEXT DEFAULT 't',
    PRIMARY KEY (obra_id, ler));
"""

REQUISITOS_INICIALES = [
    ("empresa", "Seguro de responsabilidad civil (póliza y recibo en vigor)", 12, 1),
    ("empresa", "Inscripción en el REA (Registro de Empresas Acreditadas)", 36, 1),
    ("empresa", "Certificado de estar al corriente con la AEAT (art. 43.1.f LGT)", 12, 1),
    ("empresa", "Certificado de estar al corriente con la Seguridad Social", 1, 1),
    ("empresa", "RNT / TC2 del último mes", 1, 0),
    ("empresa", "Evaluación de riesgos y planificación preventiva", 12, 1),
    ("empresa", "Adhesión al plan de seguridad y salud de la obra", None, 1),
    ("empresa", "Modalidad preventiva (servicio de prevención)", 12, 1),
    ("trabajador", "Formación PRL (convenio de la construcción, TPC)", None, 1),
    ("trabajador", "Aptitud médica (reconocimiento médico)", 12, 1),
    ("trabajador", "Entrega de EPI", 12, 1),
    ("trabajador", "Alta en la Seguridad Social (ITA/IDC)", None, 1),
]
LER_FRECUENTES = {
    "170101": "Hormigón", "170102": "Ladrillos", "170103": "Tejas y materiales cerámicos",
    "170107": "Mezclas de hormigón, ladrillos, tejas y cerámicos", "170201": "Madera", "170202": "Vidrio", "170203": "Plástico",
    "170302": "Mezclas bituminosas", "170405": "Hierro y acero", "170407": "Metales mezclados", "170411": "Cables",
    "170504": "Tierras y piedras", "170604": "Materiales de aislamiento", "170802": "Materiales a base de yeso",
    "170904": "Residuos mezclados de construcción y demolición", "150101": "Envases de papel y cartón", "150102": "Envases de plástico",
    "150110*": "Envases con restos de sustancias peligrosas", "170605*": "Materiales de construcción con amianto",
}


def init(con):
    con.executescript(SCHEMA)
    if not db.one(con, "SELECT id FROM cae_requisitos LIMIT 1"):
        for amb, nom, meses, bloq in REQUISITOS_INICIALES:
            con.execute("INSERT OR IGNORE INTO cae_requisitos (ambito, nombre, meses_validez, bloquea_acceso) VALUES (?,?,?,?)", (amb, nom, meses, bloq))
    con.commit()


def carpeta(sub: str) -> Path:
    p = DATA_DIR / "prevencion" / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def _guardar_archivo(sub: str, nombre: str, data: bytes) -> tuple[str, str]:
    h = hashlib.sha256(data).hexdigest()
    ruta = carpeta(sub) / f"{h[:16]}{Path(nombre).suffix.lower() or '.pdf'}"
    if not ruta.exists():
        ruta.write_bytes(data)
    return str(ruta), h


# ============================================================================ requisitos y documentos
def requisitos(con, ambito: str | None = None, solo_activos: bool = True) -> list[dict]:
    q, p = "SELECT * FROM cae_requisitos WHERE 1=1", []
    if ambito:
        q += " AND ambito=?"; p.append(ambito)
    if solo_activos:
        q += " AND activo=1"
    return db.rows(con, q + " ORDER BY ambito, id", p)


def subir_documento(con, requisito_id: int, usuario: str, proveedor_id: int | None = None, trabajador_id: int | None = None,
                    fecha_emision: str | None = None, fecha_caducidad: str | None = None, nombre: str = "", data: bytes | None = None) -> int:
    req = db.one(con, "SELECT * FROM cae_requisitos WHERE id=?", (requisito_id,))
    if not req:
        raise ValueError("Requisito desconocido.")
    if req["ambito"] == "empresa" and not proveedor_id:
        raise ValueError("Indique la empresa.")
    if req["ambito"] == "trabajador" and not trabajador_id:
        raise ValueError("Indique el trabajador.")
    if not fecha_caducidad and req["meses_validez"] and fecha_emision:
        fe = date.fromisoformat(fecha_emision)
        m = fe.month - 1 + req["meses_validez"]
        y = fe.year + m // 12
        m = m % 12 + 1
        import calendar
        fecha_caducidad = date(y, m, min(fe.day, calendar.monthrange(y, m)[1])).isoformat()
    ruta = h = None
    if data:
        ruta, h = _guardar_archivo("cae", nombre, data)
    with db.tx(con):
        cur = con.execute("""INSERT INTO cae_documentos (requisito_id, proveedor_id, trabajador_id, fecha_emision, fecha_caducidad, archivo, sha256,
                             subido_por, subido_en) VALUES (?,?,?,?,?,?,?,?,?)""",
                          (requisito_id, proveedor_id if req["ambito"] == "empresa" else None, trabajador_id if req["ambito"] == "trabajador" else None,
                           fecha_emision, fecha_caducidad, ruta, h, usuario, db.now_iso()))
        db.audit(con, usuario, "cae_documento", "cae_documento", cur.lastrowid, {"requisito": req["nombre"], "caduca": fecha_caducidad})
    return cur.lastrowid


def validar_documento(con, doc_id: int, usuario: str, aceptar: bool, motivo: str = "") -> None:
    d = db.one(con, "SELECT * FROM cae_documentos WHERE id=?", (doc_id,))
    if not aceptar and not motivo.strip():
        raise ValueError("Indique el motivo del rechazo.")
    if d["subido_por"] == usuario and db.get_setting(con, "cae_cuatro_ojos", "0") == "1":
        raise ValueError("Cuatro ojos: valida una persona distinta de quien subió el documento.")
    with db.tx(con):
        con.execute("UPDATE cae_documentos SET estado=?, validado_por=?, validado_en=?, motivo_rechazo=? WHERE id=?",
                    ("validado" if aceptar else "rechazado", usuario, db.now_iso(), motivo.strip() or None, doc_id))
        db.audit(con, usuario, "cae_validacion", "cae_documento", doc_id, {"aceptado": aceptar, "motivo": motivo})


def _doc_vigente(con, requisito_id: int, proveedor_id=None, trabajador_id=None, fecha: date | None = None) -> dict | None:
    fecha = (fecha or date.today()).isoformat()
    campo, val = ("proveedor_id", proveedor_id) if proveedor_id else ("trabajador_id", trabajador_id)
    return db.one(con, f"""SELECT * FROM cae_documentos WHERE requisito_id=? AND {campo}=? AND estado='validado'
                           AND (fecha_emision IS NULL OR fecha_emision<=?) AND (fecha_caducidad IS NULL OR fecha_caducidad>=?)
                           ORDER BY COALESCE(fecha_caducidad,'9999') DESC LIMIT 1""", (requisito_id, val, fecha, fecha))


def estado_empresa(con, proveedor_id: int, fecha: date | None = None) -> list[dict]:
    out = []
    for r in requisitos(con, "empresa"):
        d = _doc_vigente(con, r["id"], proveedor_id=proveedor_id, fecha=fecha)
        pend = None if d else db.one(con, """SELECT estado FROM cae_documentos WHERE requisito_id=? AND proveedor_id=? ORDER BY id DESC LIMIT 1""",
                                     (r["id"], proveedor_id))
        out.append({"requisito_id": r["id"], "requisito": r["nombre"], "obligatorio": r["obligatorio"], "bloquea": r["bloquea_acceso"],
                    "ok": bool(d), "caduca": d["fecha_caducidad"] if d else None,
                    "situacion": "Vigente" if d else ({"pendiente_validar": "Pendiente de validar", "rechazado": "Rechazado"}.get(pend["estado"], "Caducado")
                                                      if pend else "Falta")})
    return out


def estado_trabajador(con, trabajador_id: int, fecha: date | None = None) -> list[dict]:
    out = []
    for r in requisitos(con, "trabajador"):
        d = _doc_vigente(con, r["id"], trabajador_id=trabajador_id, fecha=fecha)
        out.append({"requisito_id": r["id"], "requisito": r["nombre"], "obligatorio": r["obligatorio"], "bloquea": r["bloquea_acceso"],
                    "ok": bool(d), "caduca": d["fecha_caducidad"] if d else None, "situacion": "Vigente" if d else "Falta o caducado"})
    return out


# ============================================================================ empresas y trabajadores en obra
def alta_empresa(con, obra_id: int, proveedor_id: int, fecha_alta: str, usuario: str, nivel: int = 1, contratista_id: int | None = None,
                 actividad: str = "", contacto: str = "", email: str = "") -> int:
    if db.one(con, "SELECT id FROM cae_empresas_obra WHERE obra_id=? AND proveedor_id=? AND fecha_baja IS NULL", (obra_id, proveedor_id)):
        raise ValueError("La empresa ya está de alta en la obra.")
    max_nivel = int(db.get_setting(con, "cae_nivel_max", "3") or 3)      # Ley 32/2006: hasta el tercer nivel de subcontratación
    if nivel > max_nivel:
        raise ValueError(f"Nivel de subcontratación {nivel} no permitido (máximo {max_nivel}, Ley 32/2006).")
    with db.tx(con):
        cur = con.execute("""INSERT INTO cae_empresas_obra (obra_id, proveedor_id, fecha_alta, nivel, contratista_id, actividad, contacto, email, usuario, creado_en)
                             VALUES (?,?,?,?,?,?,?,?,?,?)""", (obra_id, proveedor_id, fecha_alta, nivel, contratista_id, actividad, contacto, email, usuario, db.now_iso()))
        db.audit(con, usuario, "cae_alta_empresa", "obra", obra_id, {"proveedor": proveedor_id, "nivel": nivel})
    return cur.lastrowid


def baja_empresa(con, reg_id: int, fecha_baja: str, usuario: str) -> None:
    r = db.one(con, "SELECT * FROM cae_empresas_obra WHERE id=?", (reg_id,))
    with db.tx(con):
        con.execute("UPDATE cae_empresas_obra SET fecha_baja=? WHERE id=?", (fecha_baja, reg_id))
        con.execute("""UPDATE cae_trabajador_obra SET fecha_baja=? WHERE obra_id=? AND fecha_baja IS NULL
                       AND trabajador_id IN (SELECT id FROM cae_trabajadores WHERE proveedor_id=?)""", (fecha_baja, r["obra_id"], r["proveedor_id"]))
        db.audit(con, usuario, "cae_baja_empresa", "obra", r["obra_id"], {"proveedor": r["proveedor_id"], "fecha": fecha_baja})


def empresas_obra(con, obra_id: int, incluir_bajas: bool = False) -> list[dict]:
    q = """SELECT e.*, p.nombre, p.nif FROM cae_empresas_obra e JOIN proveedores p ON p.id=e.proveedor_id WHERE e.obra_id=?"""
    if not incluir_bajas:
        q += " AND e.fecha_baja IS NULL"
    out = db.rows(con, q + " ORDER BY e.nivel, p.nombre", (obra_id,))
    for e in out:
        est = estado_empresa(con, e["proveedor_id"])
        e["docs_ok"] = sum(1 for x in est if x["ok"])
        e["docs_total"] = len(est)
        e["faltan"] = [x["requisito"] for x in est if not x["ok"] and x["obligatorio"]]
        e["trabajadores"] = (db.one(con, """SELECT COUNT(*) n FROM cae_trabajador_obra t JOIN cae_trabajadores w ON w.id=t.trabajador_id
                                            WHERE t.obra_id=? AND w.proveedor_id=? AND t.fecha_baja IS NULL""", (obra_id, e["proveedor_id"])) or {}).get("n", 0)
    return out


def alta_trabajador(con, proveedor_id: int, nombre: str, dni: str, puesto: str, usuario: str) -> int:
    from .fiscal import validate_nif, normalize_nif
    dni = normalize_nif(dni)
    ok, msg = validate_nif(dni)
    if not ok:
        raise ValueError(f"DNI/NIE no válido: {msg}")
    ya = db.one(con, "SELECT id, proveedor_id FROM cae_trabajadores WHERE dni=?", (dni,))
    with db.tx(con):
        if ya:
            con.execute("UPDATE cae_trabajadores SET proveedor_id=?, nombre=?, puesto=?, activo=1 WHERE id=?", (proveedor_id, nombre.strip(), puesto, ya["id"]))
            tid = ya["id"]
        else:
            tid = con.execute("INSERT INTO cae_trabajadores (proveedor_id, nombre, dni, puesto, creado_en) VALUES (?,?,?,?,?)",
                              (proveedor_id, nombre.strip(), dni, puesto, db.now_iso())).lastrowid
        db.audit(con, usuario, "cae_trabajador", "cae_trabajador", tid, {"proveedor": proveedor_id})
    return tid


def alta_trabajador_obra(con, trabajador_id: int, obra_id: int, fecha_alta: str, usuario: str) -> None:
    w = db.one(con, "SELECT * FROM cae_trabajadores WHERE id=?", (trabajador_id,))
    if not db.one(con, "SELECT id FROM cae_empresas_obra WHERE obra_id=? AND proveedor_id=? AND fecha_baja IS NULL", (obra_id, w["proveedor_id"])):
        raise ValueError("Su empresa no está de alta en esta obra: dé de alta antes la empresa.")
    with db.tx(con):
        con.execute("INSERT OR IGNORE INTO cae_trabajador_obra (trabajador_id, obra_id, fecha_alta) VALUES (?,?,?)", (trabajador_id, obra_id, fecha_alta))
        db.audit(con, usuario, "cae_alta_trabajador_obra", "obra", obra_id, {"trabajador": trabajador_id})


def baja_trabajador_obra(con, trabajador_id: int, obra_id: int, fecha_baja: str, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE cae_trabajador_obra SET fecha_baja=? WHERE trabajador_id=? AND obra_id=? AND fecha_baja IS NULL", (fecha_baja, trabajador_id, obra_id))
        db.audit(con, usuario, "cae_baja_trabajador_obra", "obra", obra_id, {"trabajador": trabajador_id})


def trabajadores_obra(con, obra_id: int) -> list[dict]:
    return db.rows(con, """SELECT w.*, p.nombre AS empresa, t.fecha_alta, t.fecha_baja FROM cae_trabajador_obra t
                           JOIN cae_trabajadores w ON w.id=t.trabajador_id JOIN proveedores p ON p.id=w.proveedor_id
                           WHERE t.obra_id=? AND t.fecha_baja IS NULL ORDER BY p.nombre, w.nombre""", (obra_id,))


# ============================================================================ control de acceso
def puede_acceder(con, obra_id: int, dni: str, fecha: date | None = None, registrar: bool = False, usuario: str = "") -> dict:
    from .fiscal import normalize_nif
    fecha = fecha or date.today()
    dni = normalize_nif(dni)
    motivos = []
    w = db.one(con, "SELECT w.*, p.nombre AS empresa FROM cae_trabajadores w JOIN proveedores p ON p.id=w.proveedor_id WHERE w.dni=?", (dni,))
    if not w:
        motivos.append("Trabajador no registrado.")
    else:
        if not w["activo"]:
            motivos.append("Trabajador dado de baja.")
        f = fecha.isoformat()
        if not db.one(con, """SELECT id FROM cae_empresas_obra WHERE obra_id=? AND proveedor_id=? AND fecha_alta<=?
                              AND (fecha_baja IS NULL OR fecha_baja>=?)""", (obra_id, w["proveedor_id"], f, f)):
            motivos.append(f"La empresa {w['empresa']} no está de alta en esta obra.")
        if not db.one(con, """SELECT id FROM cae_trabajador_obra WHERE obra_id=? AND trabajador_id=? AND fecha_alta<=?
                              AND (fecha_baja IS NULL OR fecha_baja>=?)""", (obra_id, w["id"], f, f)):
            motivos.append("El trabajador no está de alta en esta obra.")
        for x in estado_empresa(con, w["proveedor_id"], fecha):
            if not x["ok"] and x["bloquea"] and x["obligatorio"]:
                motivos.append(f"Empresa: {x['requisito']} ({x['situacion'].lower()}).")
        for x in estado_trabajador(con, w["id"], fecha):
            if not x["ok"] and x["bloquea"] and x["obligatorio"]:
                motivos.append(f"Trabajador: {x['requisito']} ({x['situacion'].lower()}).")
    res = {"permitido": not motivos, "motivos": motivos, "trabajador": w}
    if registrar:
        import json
        with db.tx(con):
            con.execute("INSERT INTO cae_accesos (obra_id, trabajador_id, dni, fecha_hora, permitido, motivos, usuario) VALUES (?,?,?,?,?,?,?)",
                        (obra_id, w["id"] if w else None, dni, datetime.now().isoformat(timespec="seconds"), int(not motivos),
                         json.dumps(motivos, ensure_ascii=False), usuario))
    return res


def caducan_pronto(con, dias: int = 15) -> list[dict]:
    hoy = date.today()
    lim = (hoy + timedelta(days=dias)).isoformat()
    return db.rows(con, """SELECT d.*, r.nombre AS requisito, r.ambito, COALESCE(p.nombre, pw.nombre) AS empresa, w.nombre AS trabajador,
                                  COALESCE(d.proveedor_id, w.proveedor_id) AS empresa_id
                           FROM cae_documentos d JOIN cae_requisitos r ON r.id=d.requisito_id
                           LEFT JOIN proveedores p ON p.id=d.proveedor_id LEFT JOIN cae_trabajadores w ON w.id=d.trabajador_id
                           LEFT JOIN proveedores pw ON pw.id=w.proveedor_id
                           WHERE d.estado='validado' AND d.fecha_caducidad IS NOT NULL AND d.fecha_caducidad<=?
                             AND NOT EXISTS (SELECT 1 FROM cae_documentos n WHERE n.requisito_id=d.requisito_id
                                             AND COALESCE(n.proveedor_id,0)=COALESCE(d.proveedor_id,0) AND COALESCE(n.trabajador_id,0)=COALESCE(d.trabajador_id,0)
                                             AND n.id<>d.id AND n.estado<>'rechazado' AND COALESCE(n.fecha_caducidad,'9999')>d.fecha_caducidad)
                           ORDER BY d.fecha_caducidad""", (lim,))


def empresas_bloqueadas(con) -> list[dict]:
    """Empresas de alta en alguna obra con documentación obligatoria que falta o está caducada."""
    out = []
    for e in db.rows(con, "SELECT DISTINCT e.proveedor_id, p.nombre FROM cae_empresas_obra e JOIN proveedores p ON p.id=e.proveedor_id WHERE e.fecha_baja IS NULL"):
        faltan = [x["requisito"] for x in estado_empresa(con, e["proveedor_id"]) if not x["ok"] and x["obligatorio"]]
        if faltan:
            out.append({**e, "faltan": faltan})
    return out


def aeat_caducado(con, proveedor_id: int) -> bool:
    """¿Falta el certificado de estar al corriente con la AEAT (contratistas y subcontratistas, art. 43.1.f LGT)?"""
    r = db.one(con, "SELECT id FROM cae_requisitos WHERE ambito='empresa' AND nombre LIKE '%AEAT%' AND activo=1")
    if not r or not db.one(con, "SELECT id FROM cae_empresas_obra WHERE proveedor_id=?", (proveedor_id,)):
        return False                                     # solo aplica a empresas dadas de alta en CAE (subcontratas)
    return not _doc_vigente(con, r["id"], proveedor_id=proveedor_id)


def solicitar_documentacion(con, proveedor_id: int, obra_id: int | None, usuario: str) -> int:
    """Prepara el correo a la empresa con lo que le falta o caduca."""
    from . import correo
    faltan = [x for x in estado_empresa(con, proveedor_id) if not x["ok"] and x["obligatorio"]]
    trab = db.rows(con, "SELECT id, nombre FROM cae_trabajadores WHERE proveedor_id=? AND activo=1", (proveedor_id,))
    lineas = [f"  - {x['requisito']} ({x['situacion'].lower()})" for x in faltan]
    for t in trab:
        for x in estado_trabajador(con, t["id"]):
            if not x["ok"] and x["obligatorio"]:
                lineas.append(f"  - {t['nombre']}: {x['requisito']}")
    if not lineas:
        raise ValueError("A esta empresa no le falta documentación.")
    e = db.one(con, "SELECT email FROM cae_empresas_obra WHERE proveedor_id=? AND email IS NOT NULL AND email<>'' ORDER BY id DESC LIMIT 1", (proveedor_id,))
    para = (e and e["email"]) or correo.email_proveedor(con, proveedor_id)
    if not para:
        raise ValueError("No hay correo de contacto de la empresa (anótelo en su alta en obra).")
    p = db.one(con, "SELECT nombre FROM proveedores WHERE id=?", (proveedor_id,))
    cuerpo = (f"Estimados señores de {p['nombre']}:\n\nPara poder acceder a la obra y tramitar sus certificaciones y pagos necesitamos la "
              f"siguiente documentación de coordinación de actividades empresariales, que falta o está caducada:\n\n" + "\n".join(lineas) +
              "\n\nMientras no se reciba y valide, el acceso a obra del personal afectado quedará bloqueado.\n\nUn saludo.")
    return correo.preparar(con, "cae", para, f"Documentación CAE pendiente · {p['nombre']}", cuerpo, usuario, origen="cae",
                           origen_id=proveedor_id, obra_id=obra_id, evitar_duplicado=False)


# ============================================================================ residuos
def registrar_retirada(con, obra_id: int, datos: dict, usuario: str, albaran_archivo: tuple[str, bytes] | None = None,
                       certificado_archivo: tuple[str, bytes] | None = None) -> int:
    ler = re.sub(r"\s", "", str(datos.get("ler") or ""))
    if not re.match(r"^\d{6}\*?$", ler):
        raise ValueError("Código LER no válido (6 cifras, con * si es peligroso).")
    try:
        cant = Decimal(str(datos.get("cantidad") or "0").replace(",", "."))
    except Exception:
        raise ValueError("Cantidad no válida.")
    if cant <= 0:
        raise ValueError("La cantidad debe ser positiva.")
    alb = albaran_archivo and _guardar_archivo("residuos", albaran_archivo[0], albaran_archivo[1])[0]
    cer = certificado_archivo and _guardar_archivo("residuos", certificado_archivo[0], certificado_archivo[1])[0]
    with db.tx(con):
        cur = con.execute("""INSERT INTO residuos (obra_id, fecha, ler, descripcion, cantidad, unidad, transportista, gestor, gestor_nima, albaran,
                             albaran_archivo, certificado, certificado_archivo, certificado_fecha, notas, usuario, creado_en)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (obra_id, datos.get("fecha") or date.today().isoformat(), ler, datos.get("descripcion") or LER_FRECUENTES.get(ler, ""),
                           str(cant), datos.get("unidad") or "t", datos.get("transportista"), datos.get("gestor"), datos.get("gestor_nima"),
                           datos.get("albaran"), alb, datos.get("certificado"), cer, datos.get("certificado_fecha"), datos.get("notas"), usuario, db.now_iso()))
        rid = cur.lastrowid
        db.audit(con, usuario, "residuo_retirada", "obra", obra_id, {"ler": ler, "cantidad": str(cant), "albaran": datos.get("albaran")})
    enlazar_facturas(con, obra_id)
    return rid


def completar_retirada(con, rid: int, usuario: str, certificado: str | None = None, certificado_fecha: str | None = None,
                       certificado_archivo: tuple[str, bytes] | None = None, documento_id: int | None = None) -> None:
    cer = certificado_archivo and _guardar_archivo("residuos", certificado_archivo[0], certificado_archivo[1])[0]
    with db.tx(con):
        con.execute("""UPDATE residuos SET certificado=COALESCE(?, certificado), certificado_fecha=COALESCE(?, certificado_fecha),
                       certificado_archivo=COALESCE(?, certificado_archivo), documento_id=COALESCE(?, documento_id) WHERE id=?""",
                    (certificado, certificado_fecha, cer, documento_id, rid))
        db.audit(con, usuario, "residuo_completado", "residuo", rid, {"certificado": certificado, "factura": documento_id})


def _norm_alb(s: str) -> str:
    """«Alb. nº 00123», «ALB-123» y «123» son el mismo albarán."""
    t = re.sub(r"[^A-Z0-9]", "", str(s or "").upper().replace("Nº", "").replace("NO.", ""))
    t = re.sub(r"^(ALBARAN|ALBAR|ALB)", "", t)
    return t.lstrip("0")


def enlazar_facturas(con, obra_id: int | None = None) -> int:
    """Propone la factura de cada retirada: la que cita su nº de albarán en las líneas o en el campo de albaranes."""
    q = "SELECT * FROM residuos WHERE documento_id IS NULL AND albaran IS NOT NULL AND albaran<>''"
    p = []
    if obra_id:
        q += " AND obra_id=?"; p.append(obra_id)
    n = 0
    for r in db.rows(con, q, p):
        a = _norm_alb(r["albaran"])
        if len(a) < 3:
            continue
        for d in db.rows(con, """SELECT DISTINCT d.id, d.albaranes, l.albaran FROM documentos d LEFT JOIN lineas l ON l.documento_id=d.id
                                 WHERE d.obra_id=? AND d.tipo_documento IN ('factura','abono') AND d.estado NOT IN ('rechazada','eliminado','duplicado')
                                   AND (COALESCE(l.albaran,'')<>'' OR COALESCE(d.albaranes,'')<>'')""", (r["obra_id"],)):
            textos = [d["albaran"] or ""] + re.split(r"[,;/\s]+", d["albaranes"] or "")
            if any(_norm_alb(t) == a for t in textos if t):
                con.execute("UPDATE residuos SET documento_id=? WHERE id=?", (d["id"], r["id"]))
                n += 1
                break
    con.commit()
    return n


def retiradas(con, obra_id: int) -> list[dict]:
    out = db.rows(con, """SELECT r.*, d.numero AS factura_numero FROM residuos r LEFT JOIN documentos d ON d.id=r.documento_id
                          WHERE r.obra_id=? ORDER BY r.fecha DESC, r.id DESC""", (obra_id,))
    for r in out:
        falta = [x for x, ok in (("albarán", r["albaran"]), ("factura", r["documento_id"]), ("certificado del gestor", r["certificado"] or r["certificado_archivo"])) if not ok]
        r["trazabilidad"] = "Completa" if not falta else "Falta: " + ", ".join(falta)
    return out


def sin_certificado(con, dias: int | None = None) -> list[dict]:
    dias = dias if dias is not None else int(db.get_setting(con, "residuos_dias_certificado", "30") or 30)
    lim = (date.today() - timedelta(days=dias)).isoformat()
    return db.rows(con, """SELECT * FROM residuos WHERE COALESCE(certificado,'')='' AND COALESCE(certificado_archivo,'')='' AND fecha<?""", (lim,))


def resumen_ler(con, obra_id: int) -> list[dict]:
    real = {}
    for r in db.rows(con, "SELECT ler, descripcion, cantidad, unidad FROM residuos WHERE obra_id=?", (obra_id,)):
        k = (r["ler"], r["unidad"])
        real.setdefault(k, {"ler": r["ler"], "descripcion": r["descripcion"], "unidad": r["unidad"], "real": Decimal(0)})
        real[k]["real"] += Decimal(r["cantidad"])
    for p in db.rows(con, "SELECT * FROM residuos_previstos WHERE obra_id=?", (obra_id,)):
        k = (p["ler"], p["unidad"])
        real.setdefault(k, {"ler": p["ler"], "descripcion": p["descripcion"], "unidad": p["unidad"], "real": Decimal(0)})
        real[k]["previsto"] = Decimal(p["cantidad"] or 0)
    out = []
    for v in real.values():
        prev = v.get("previsto")
        out.append({**v, "previsto": prev, "desviacion_pct": float((v["real"] - prev) / prev * 100) if prev else None})
    return sorted(out, key=lambda x: x["ler"])


def guardar_previstos(con, obra_id: int, filas: list[dict], usuario: str) -> None:
    with db.tx(con):
        con.execute("DELETE FROM residuos_previstos WHERE obra_id=?", (obra_id,))
        for f in filas:
            ler = re.sub(r"\s", "", str(f.get("ler") or ""))
            if re.match(r"^\d{6}\*?$", ler) and str(f.get("cantidad") or "").strip():
                con.execute("INSERT OR REPLACE INTO residuos_previstos (obra_id, ler, descripcion, cantidad, unidad) VALUES (?,?,?,?,?)",
                            (obra_id, ler, f.get("descripcion") or LER_FRECUENTES.get(ler, ""), str(f["cantidad"]).replace(",", "."), f.get("unidad") or "t"))
        db.audit(con, usuario, "residuos_previstos", "obra", obra_id, {"filas": len(filas)})
