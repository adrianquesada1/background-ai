"""
Sesiones persistentes (servidor multiusuario).

- Al acceder se genera un token aleatorio (256 bits). En la base de datos solo se guarda su huella SHA-256.
- El navegador lo conserva en una cookie: recargar la página (F5) o volver otro día no obliga a entrar de nuevo
  mientras la sesión esté vigente (12 h, o 30 días con «Recordarme»).
- Caducidad deslizante: cada uso la renueva. El administrador ve las sesiones abiertas y puede cerrarlas.
- Todos los usuarios trabajan sobre la misma base de datos del servidor: ven exactamente lo mismo.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from . import db

COOKIE = "llorca_sesion"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sesiones (
    huella TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    creada TEXT, ultima TEXT, expira TEXT, recordar INTEGER DEFAULT 0,
    navegador TEXT, ip TEXT
);
"""


def init(con):
    con.executescript(SCHEMA)
    try:
        con.execute("ALTER TABLE sesiones ADD COLUMN ultima_pagina TEXT")
    except Exception:
        pass
    con.commit()


def registrar_pagina(con, token: str | None, pagina: str) -> None:
    if token:
        con.execute("UPDATE sesiones SET ultima_pagina=?, ultima=? WHERE huella=?",
                    (pagina, datetime.now().isoformat(timespec="seconds"), _huella(token)))
        con.commit()


def _huella(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _duracion(recordar: bool) -> timedelta:
    return timedelta(days=30) if recordar else timedelta(hours=12)


def crear(con, usuario_id: int, recordar: bool, navegador: str = "", ip: str = "") -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    ahora = datetime.now()
    with db.tx(con):
        con.execute("INSERT INTO sesiones (huella, usuario_id, creada, ultima, expira, recordar, navegador, ip) VALUES (?,?,?,?,?,?,?,?)",
                    (_huella(token), usuario_id, ahora.isoformat(timespec="seconds"), ahora.isoformat(timespec="seconds"),
                     (ahora + _duracion(recordar)).isoformat(timespec="seconds"), int(recordar), navegador[:200], ip[:60]))
    return token, int(_duracion(recordar).total_seconds())


def validar(con, token: str | None) -> dict | None:
    if not token:
        return None
    r = db.one(con, """SELECT s.*, u.usuario, u.nombre, u.rol, u.activo FROM sesiones s JOIN usuarios u ON u.id=s.usuario_id
                       WHERE s.huella=?""", (_huella(token),))
    ahora = datetime.now()
    if not r or not r["activo"] or r["expira"] < ahora.isoformat(timespec="seconds"):
        return None
    con.execute("UPDATE sesiones SET ultima=?, expira=? WHERE huella=?",
                (ahora.isoformat(timespec="seconds"), (ahora + _duracion(bool(r["recordar"]))).isoformat(timespec="seconds"), r["huella"]))
    con.commit()
    return {"id": r["usuario_id"], "usuario": r["usuario"], "nombre": r["nombre"] or r["usuario"], "rol": r["rol"]}


def cerrar(con, token: str | None) -> None:
    if token:
        con.execute("DELETE FROM sesiones WHERE huella=?", (_huella(token),))
        con.commit()


def cerrar_de_usuario(con, usuario_id: int) -> None:
    con.execute("DELETE FROM sesiones WHERE usuario_id=?", (usuario_id,))
    con.commit()


def activas(con) -> list[dict]:
    con.execute("DELETE FROM sesiones WHERE expira < ?", (datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    return db.rows(con, """SELECT substr(s.huella,1,10) AS id, u.usuario, u.nombre, u.rol, s.creada, s.ultima, s.expira,
                                  s.recordar, s.ultima_pagina, s.navegador, s.ip, s.huella
                           FROM sesiones s JOIN usuarios u ON u.id=s.usuario_id ORDER BY s.ultima DESC""")


def revocar(con, huella: str) -> None:
    con.execute("DELETE FROM sesiones WHERE huella=?", (huella,))
    con.commit()


def js_cookie(token: str | None, segundos: int = 0) -> str:
    """Script que guarda (o borra) la cookie en el navegador. SameSite=Lax: no se envía a otros sitios."""
    if token:
        valor = f"{COOKIE}={token}; max-age={segundos}; path=/; SameSite=Lax"
    else:
        valor = f"{COOKIE}=; max-age=0; path=/; SameSite=Lax"
    return ("<script>try{window.parent.document.cookie=%r;}catch(e){}try{document.cookie=%r;}catch(e){}</script>"
            % (valor, valor))
