"""
Usuarios y acceso.

- Contraseñas con PBKDF2-HMAC-SHA256 (200.000 iteraciones) y sal aleatoria por usuario. Nunca se guarda la contraseña.
- Roles:
    admin     -> todo, incluida la gestión de usuarios y la configuración
    gestor    -> circuito de facturas, certificaciones, contratación, maestros (sin usuarios ni configuración)
    consulta  -> solo lectura de paneles y documentos
- Bloqueo temporal tras 5 intentos fallidos (10 minutos). Todo acceso queda en la auditoría.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from . import db

ROLES = {"admin": "Administrador", "direccion": "Dirección", "gestor": "Administración / Finanzas",
         "jefe_obra": "Jefe de obra", "tecnico": "Técnico / Estudios", "consulta": "Consulta"}
DESCRIPCION_ROLES = {
    "admin": "Todo, incluidos usuarios, permisos y configuración.",
    "direccion": "Ve todas las obras y aprueba facturas de cualquier importe (también las que superan el umbral).",
    "gestor": "Circuito completo de facturas, pagos, proveedores y certificaciones. Aprueba hasta el umbral.",
    "jefe_obra": "Solo sus obras: da la conformidad a las facturas, consulta rentabilidad, certificaciones y contratación.",
    "tecnico": "Certificaciones, contratación, obras y partidas, y consulta de costes.",
    "consulta": "Solo lectura de paneles y documentos.",
}
ITER = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS obra_usuarios (
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    PRIMARY KEY (obra_id, usuario_id)
);
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY,
    usuario TEXT UNIQUE NOT NULL,
    nombre TEXT,
    rol TEXT NOT NULL DEFAULT 'gestor',
    sal TEXT NOT NULL, hash TEXT NOT NULL,
    activo INTEGER DEFAULT 1,
    intentos INTEGER DEFAULT 0, bloqueado_hasta TEXT,
    creado_en TEXT, ultimo_acceso TEXT
);
"""


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def _hash(clave: str, sal: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), bytes.fromhex(sal), ITER).hex()


def hay_usuarios(con) -> bool:
    return bool(db.one(con, "SELECT COUNT(*) n FROM usuarios")["n"])


def validar_clave(clave: str) -> str | None:
    if len(clave or "") < 8:
        return "La contraseña debe tener al menos 8 caracteres."
    if clave.isdigit() or clave.isalpha():
        return "La contraseña debe combinar letras y números."
    return None


def crear(con, usuario: str, nombre: str, clave: str, rol: str, por: str) -> None:
    usuario = usuario.strip().lower()
    if not usuario or " " in usuario:
        raise ValueError("Usuario no válido (sin espacios).")
    err = validar_clave(clave)
    if err:
        raise ValueError(err)
    if rol not in ROLES:
        raise ValueError("Rol no válido.")
    sal = secrets.token_hex(16)
    with db.tx(con):
        con.execute("INSERT INTO usuarios (usuario, nombre, rol, sal, hash, creado_en) VALUES (?,?,?,?,?,?)",
                    (usuario, nombre.strip(), rol, sal, _hash(clave, sal), db.now_iso()))
        db.audit(con, por, "crear_usuario", "usuario", None, {"usuario": usuario, "rol": rol})


def autenticar(con, usuario: str, clave: str) -> tuple[dict | None, str]:
    u = db.one(con, "SELECT * FROM usuarios WHERE usuario=?", ((usuario or "").strip().lower(),))
    if not u or not u["activo"]:
        db.audit(con, usuario, "acceso_fallido", "usuario", None, {"motivo": "no existe o inactivo"}); con.commit()
        return None, "Usuario o contraseña incorrectos."
    if u["bloqueado_hasta"] and u["bloqueado_hasta"] > db.now_iso():
        return None, "Usuario bloqueado temporalmente por intentos fallidos. Pruebe en unos minutos."
    if not hmac.compare_digest(_hash(clave or "", u["sal"]), u["hash"]):
        intentos = (u["intentos"] or 0) + 1
        bloqueo = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds") if intentos >= 5 else None
        con.execute("UPDATE usuarios SET intentos=?, bloqueado_hasta=? WHERE id=?", (0 if bloqueo else intentos, bloqueo, u["id"]))
        db.audit(con, u["usuario"], "acceso_fallido", "usuario", u["id"], {"intentos": intentos}); con.commit()
        return None, "Usuario o contraseña incorrectos."
    con.execute("UPDATE usuarios SET intentos=0, bloqueado_hasta=NULL, ultimo_acceso=? WHERE id=?", (db.now_iso(), u["id"]))
    db.audit(con, u["usuario"], "acceso", "usuario", u["id"], None); con.commit()
    return {"id": u["id"], "usuario": u["usuario"], "nombre": u["nombre"] or u["usuario"], "rol": u["rol"]}, ""


def cambiar_clave(con, usuario_id: int, clave: str, por: str) -> None:
    err = validar_clave(clave)
    if err:
        raise ValueError(err)
    sal = secrets.token_hex(16)
    with db.tx(con):
        con.execute("UPDATE usuarios SET sal=?, hash=?, intentos=0, bloqueado_hasta=NULL WHERE id=?", (sal, _hash(clave, sal), usuario_id))
        db.audit(con, por, "cambiar_clave", "usuario", usuario_id, None)


def eliminar(con, usuario_id: int, por: str) -> None:
    u = db.one(con, "SELECT rol FROM usuarios WHERE id=?", (usuario_id,))
    if u and u["rol"] == "admin" and db.one(con, "SELECT COUNT(*) n FROM usuarios WHERE rol='admin' AND activo=1 AND id<>?",
                                            (usuario_id,))["n"] == 0:
        raise ValueError("No se puede eliminar el último administrador.")
    with db.tx(con):
        con.execute("DELETE FROM usuarios WHERE id=?", (usuario_id,))
        db.audit(con, por, "eliminar_usuario", "usuario", usuario_id, None)


def listar(con) -> list[dict]:
    return db.rows(con, "SELECT id, usuario, nombre, rol, activo, ultimo_acceso, creado_en FROM usuarios ORDER BY usuario")


def actualizar(con, usuario_id: int, rol: str, activo: bool, por: str) -> None:
    admins = db.one(con, "SELECT COUNT(*) n FROM usuarios WHERE rol='admin' AND activo=1 AND id<>?", (usuario_id,))["n"]
    if (rol != "admin" or not activo) and admins == 0:
        raise ValueError("Debe quedar al menos un administrador activo.")
    with db.tx(con):
        con.execute("UPDATE usuarios SET rol=?, activo=? WHERE id=?", (rol, int(activo), usuario_id))
        db.audit(con, por, "actualizar_usuario", "usuario", usuario_id, {"rol": rol, "activo": activo})



def obras_de(con, usuario_id: int) -> set[int]:
    return {r["obra_id"] for r in db.rows(con, "SELECT obra_id FROM obra_usuarios WHERE usuario_id=?", (usuario_id,))}


def asignar_obras(con, obra_id: int, usuario_ids: list[int], por: str) -> None:
    with db.tx(con):
        con.execute("DELETE FROM obra_usuarios WHERE obra_id=?", (obra_id,))
        for u in usuario_ids:
            con.execute("INSERT INTO obra_usuarios (obra_id, usuario_id) VALUES (?,?)", (obra_id, u))
        db.audit(con, por, "asignar_responsables_obra", "obra", obra_id, {"usuarios": usuario_ids})


def jefes_de_obra(con, obra_id: int) -> list[dict]:
    return db.rows(con, """SELECT u.id, u.usuario, u.nombre, u.rol FROM obra_usuarios ou JOIN usuarios u ON u.id=ou.usuario_id
                           WHERE ou.obra_id=? AND u.activo=1 AND u.rol='jefe_obra'""", (obra_id,))



# ============================================================================ preferencias de cada usuario
def _init_prefs(con):
    con.execute("CREATE TABLE IF NOT EXISTS preferencias_usuario (usuario_id INTEGER NOT NULL, clave TEXT NOT NULL, valor TEXT, "
                "actualizado_en TEXT, PRIMARY KEY (usuario_id, clave))")


def _codificar(v):
    import json
    from datetime import date, datetime as _dt
    if isinstance(v, _dt):
        return json.dumps({"t": "datetime", "v": v.isoformat()})
    if isinstance(v, date):
        return json.dumps({"t": "date", "v": v.isoformat()})
    if isinstance(v, tuple):
        return json.dumps({"t": "tuple", "v": [_codificar(x) for x in v]})
    try:
        return json.dumps({"t": "json", "v": v}, ensure_ascii=False)
    except TypeError:
        return None


def _decodificar(txt):
    import json
    from datetime import date, datetime as _dt
    o = json.loads(txt)
    if o["t"] == "date":
        return date.fromisoformat(o["v"])
    if o["t"] == "datetime":
        return _dt.fromisoformat(o["v"])
    if o["t"] == "tuple":
        return tuple(_decodificar(x) for x in o["v"])
    return o["v"]


def cargar_preferencias(con, usuario_id: int) -> dict:
    _init_prefs(con)
    out = {}
    for r in db.rows(con, "SELECT clave, valor FROM preferencias_usuario WHERE usuario_id=?", (usuario_id,)):
        try:
            out[r["clave"]] = _decodificar(r["valor"])
        except Exception:
            pass
    return out


def guardar_preferencias(con, usuario_id: int, prefs: dict) -> int:
    """Guarda solo lo que ha cambiado. Devuelve cuántas claves se han actualizado."""
    _init_prefs(con)
    n = 0
    for k, v in prefs.items():
        cod = _codificar(v)
        if cod is None:
            continue
        cur = con.execute("INSERT INTO preferencias_usuario (usuario_id, clave, valor, actualizado_en) VALUES (?,?,?,?) "
                          "ON CONFLICT(usuario_id, clave) DO UPDATE SET valor=excluded.valor, actualizado_en=excluded.actualizado_en "
                          "WHERE preferencias_usuario.valor IS NOT excluded.valor", (usuario_id, k, cod, db.now_iso()))
        n += cur.rowcount
    if n:
        con.commit()
    return n


def borrar_preferencias(con, usuario_id: int) -> None:
    _init_prefs(con)
    con.execute("DELETE FROM preferencias_usuario WHERE usuario_id=?", (usuario_id,))
    con.commit()
