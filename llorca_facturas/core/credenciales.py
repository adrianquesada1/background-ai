"""
Contraseñas y claves de servicios externos (buzón, correo saliente, SIS).

Orden de búsqueda de cada secreto:
  1. Variable de entorno LLORCA_<NOMBRE> (p. ej. LLORCA_BUZON_CLAVE). Recomendado en el servidor.
  2. Guardado desde Configuración: se cifra con una clave local (Fernet, AES-128 + HMAC) que vive en un archivo aparte
     (`data/.clave_secretos`). Una copia de la base de datos sin ese archivo no revela ninguna contraseña.

Para Microsoft 365 (que ya no admite usuario y contraseña en IMAP/SMTP) se obtiene un token OAuth2 de aplicación
(client credentials) y se cachea hasta que caduca.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import db
from .config import DATA_DIR

_CLAVE = DATA_DIR / ".clave_secretos"
_PREFIJO = "secreto_"


def _fernet():
    from cryptography.fernet import Fernet
    if not _CLAVE.exists():
        _CLAVE.write_bytes(Fernet.generate_key())
        try:
            os.chmod(_CLAVE, 0o600)
        except Exception:
            pass
    return Fernet(_CLAVE.read_bytes().strip())


def guardar(con, nombre: str, valor: str, usuario: str = "") -> None:
    if not valor:
        db.set_setting(con, _PREFIJO + nombre, "")
    else:
        db.set_setting(con, _PREFIJO + nombre, _fernet().encrypt(valor.encode()).decode())
    db.audit(con, usuario or "sistema", "guardar_secreto", "ajustes", None, {"nombre": nombre, "vacio": not valor})
    con.commit()


def leer(con, nombre: str) -> str:
    env = os.environ.get("LLORCA_" + nombre.upper())
    if env:
        return env
    v = db.get_setting(con, _PREFIJO + nombre, "")
    if not v:
        return ""
    try:
        return _fernet().decrypt(v.encode()).decode()
    except Exception:
        return ""          # clave local perdida o dato dañado: se trata como no configurado


def hay(con, nombre: str) -> bool:
    return bool(leer(con, nombre))


def origen(con, nombre: str) -> str:
    if os.environ.get("LLORCA_" + nombre.upper()):
        return f"variable de entorno LLORCA_{nombre.upper()}"
    return "guardada (cifrada) en esta instalación" if db.get_setting(con, _PREFIJO + nombre, "") else "no configurada"


# ============================================================================ OAuth2 Microsoft 365
_tokens: dict[tuple, tuple[str, float]] = {}
_lock = threading.Lock()


def token_m365(tenant: str, client_id: str, client_secret: str, recurso: str = "https://outlook.office365.com/.default") -> str:
    """Token de aplicación (client credentials) para IMAP/SMTP de Exchange Online."""
    import requests
    clave = (tenant, client_id, recurso)
    with _lock:
        t = _tokens.get(clave)
        if t and t[1] > time.time() + 60:
            return t[0]
        r = requests.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                          data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
                                "scope": recurso}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Microsoft 365 no concede el token ({r.status_code}): {r.text[:300]}")
        j = r.json()
        _tokens[clave] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
        return j["access_token"]


def cadena_xoauth2(usuario: str, token: str) -> str:
    return f"user={usuario}\x01auth=Bearer {token}\x01\x01"


def ruta_clave() -> Path:
    return _CLAVE
