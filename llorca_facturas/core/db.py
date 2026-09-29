"""
Capa de datos (SQLite).

- Todos los importes se guardan en CÉNTIMOS (INTEGER) -> sin errores de coma flotante.
- Cantidades, precios unitarios y porcentajes se guardan como TEXTO decimal exacto.
- Toda modificación relevante deja traza en la tabla `auditoria` (quién, cuándo, qué).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS obras (
    id INTEGER PRIMARY KEY,
    codigo TEXT UNIQUE NOT NULL,
    nombre TEXT NOT NULL,
    cliente TEXT,
    direccion TEXT,
    alias TEXT DEFAULT '',
    presupuesto_venta_cents INTEGER DEFAULT 0,
    presupuesto_coste_cents INTEGER DEFAULT 0,
    activa INTEGER DEFAULT 1,
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS partidas (
    id INTEGER PRIMARY KEY,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    codigo TEXT NOT NULL,
    descripcion TEXT NOT NULL,
    palabras_clave TEXT DEFAULT '',
    presupuesto_coste_cents INTEGER DEFAULT 0,
    presupuesto_venta_cents INTEGER DEFAULT 0,
    UNIQUE (obra_id, codigo)
);

CREATE TABLE IF NOT EXISTS proveedores (
    id INTEGER PRIMARY KEY,
    nif TEXT UNIQUE,
    nombre TEXT NOT NULL,
    tipo TEXT DEFAULT '',
    notas TEXT DEFAULT '',
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS proveedor_ibans (
    id INTEGER PRIMARY KEY,
    proveedor_id INTEGER NOT NULL REFERENCES proveedores(id) ON DELETE CASCADE,
    iban TEXT NOT NULL,
    primera_vez TEXT,
    ultima_vez TEXT,
    veces INTEGER DEFAULT 1,
    verificado INTEGER DEFAULT 0,
    verificado_por TEXT,
    UNIQUE (proveedor_id, iban)
);

CREATE TABLE IF NOT EXISTS documentos (
    id INTEGER PRIMARY KEY,
    file_hash TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    paginas INTEGER,
    tiene_texto INTEGER DEFAULT 0,
    texto TEXT,
    estado TEXT DEFAULT 'pendiente_revision',
    tipo_documento TEXT DEFAULT 'factura',
    proveedor_id INTEGER REFERENCES proveedores(id),
    obra_id INTEGER REFERENCES obras(id),
    obra_confianza REAL,
    emisor_nombre TEXT, emisor_nif TEXT,
    receptor_nombre TEXT, receptor_nif TEXT,
    numero TEXT, numero_normalizado TEXT,
    fecha TEXT, fecha_vencimiento TEXT, periodo TEXT,
    referencia_obra_texto TEXT, pedido_contrato TEXT, presupuesto_ref TEXT,
    albaranes TEXT, factura_rectificada TEXT,
    concepto_general TEXT,
    importe_bruto_cents INTEGER,
    base_imponible_cents INTEGER,
    total_iva_cents INTEGER,
    total_recargo_cents INTEGER,
    total_factura_cents INTEGER,
    irpf_pct TEXT, irpf_cents INTEGER,
    ret_garantia_pct TEXT, ret_garantia_base_cents INTEGER, ret_garantia_cents INTEGER,
    total_a_pagar_cents INTEGER,
    inversion_sujeto_pasivo INTEGER DEFAULT 0,
    exencion_motivo TEXT,
    forma_pago TEXT, iban TEXT,
    pagada INTEGER DEFAULT 0, fecha_pago TEXT,
    ret_garantia_devuelta INTEGER DEFAULT 0,
    confianza REAL,
    campos_dudosos TEXT,
    observaciones_ia TEXT,
    extraccion_json TEXT,
    modelo TEXT, tokens_entrada INTEGER, tokens_salida INTEGER,
    extraido_en TEXT,
    revisado_por TEXT, revisado_en TEXT,
    aprobado_por TEXT, aprobado_en TEXT,
    notas TEXT DEFAULT '',
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_doc_obra ON documentos(obra_id);
CREATE INDEX IF NOT EXISTS ix_doc_prov ON documentos(proveedor_id);
CREATE INDEX IF NOT EXISTS ix_doc_num ON documentos(emisor_nif, numero_normalizado);

CREATE TABLE IF NOT EXISTS impuestos (
    id INTEGER PRIMARY KEY,
    documento_id INTEGER NOT NULL REFERENCES documentos(id) ON DELETE CASCADE,
    tipo_pct TEXT NOT NULL,
    base_cents INTEGER NOT NULL,
    cuota_cents INTEGER NOT NULL,
    recargo_pct TEXT,
    recargo_cents INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lineas (
    id INTEGER PRIMARY KEY,
    documento_id INTEGER NOT NULL REFERENCES documentos(id) ON DELETE CASCADE,
    orden INTEGER,
    codigo TEXT,
    descripcion TEXT,
    cantidad TEXT, unidad TEXT, precio_unitario TEXT, descuento_pct TEXT,
    importe_cents INTEGER NOT NULL,
    tipo_linea TEXT DEFAULT 'normal',
    es_extra INTEGER DEFAULT 0,
    albaran TEXT,
    partida_id INTEGER REFERENCES partidas(id) ON DELETE SET NULL,
    partida_origen TEXT,
    partida_confianza REAL
);
CREATE INDEX IF NOT EXISTS ix_lin_doc ON lineas(documento_id);
CREATE INDEX IF NOT EXISTS ix_lin_part ON lineas(partida_id);

CREATE TABLE IF NOT EXISTS incidencias (
    id INTEGER PRIMARY KEY,
    documento_id INTEGER NOT NULL REFERENCES documentos(id) ON DELETE CASCADE,
    codigo TEXT NOT NULL,
    severidad TEXT NOT NULL,
    mensaje TEXT NOT NULL,
    detalle TEXT,
    resuelta INTEGER DEFAULT 0,
    resuelta_por TEXT, resuelta_en TEXT, comentario TEXT,
    creado_en TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_inc_doc ON incidencias(documento_id);

CREATE TABLE IF NOT EXISTS auditoria (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    usuario TEXT,
    accion TEXT NOT NULL,
    entidad TEXT,
    entidad_id INTEGER,
    detalle TEXT
);

CREATE TABLE IF NOT EXISTS ajustes (
    clave TEXT PRIMARY KEY,
    valor TEXT
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def tx(con: sqlite3.Connection):
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def audit(con, usuario: str, accion: str, entidad: str = "", entidad_id: int | None = None, detalle=None):
    con.execute(
        "INSERT INTO auditoria (ts, usuario, accion, entidad, entidad_id, detalle) VALUES (?,?,?,?,?,?)",
        (now_iso(), usuario or "desconocido", accion, entidad, entidad_id,
         json.dumps(detalle, ensure_ascii=False, default=str) if detalle is not None else None),
    )


def get_setting(con, clave: str, default: str = "") -> str:
    r = con.execute("SELECT valor FROM ajustes WHERE clave=?", (clave,)).fetchone()
    return r["valor"] if r and r["valor"] is not None else default


def set_setting(con, clave: str, valor: str) -> None:
    con.execute("INSERT INTO ajustes(clave, valor) VALUES(?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
                (clave, valor))
    con.commit()


def rows(con, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def one(con, sql: str, params=()) -> dict | None:
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None
