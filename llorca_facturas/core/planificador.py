"""
Tareas automáticas en segundo plano: buzón de facturas, copias de seguridad, envío de correos, envío de asientos a SIS y
transcripción de actas.

- Un único hilo por proceso revisa cada 20 s qué tareas tocan.
- Aunque haya varios procesos (p. ej. dos servidores apuntando a la misma carpeta de datos), cada ejecución se reserva en
  la base de datos con un «turno» (lease): una tarea nunca corre dos veces a la vez.
- Cada ejecución deja resultado, duración y error en `tareas_programadas` y en `tareas_historial`; la pantalla de
  Configuración y el centro de alertas lo muestran. Un fallo nunca tumba el hilo: se registra y se reintenta en la
  siguiente vuelta.
"""
from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS tareas_programadas (
    nombre TEXT PRIMARY KEY, ultima_ejecucion TEXT, ultimo_ok TEXT, ultimo_error TEXT, ultimo_resultado TEXT,
    duracion_s REAL, turno TEXT, turno_hasta TEXT, forzar INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tareas_historial (
    id INTEGER PRIMARY KEY, nombre TEXT, inicio TEXT, fin TEXT, ok INTEGER, resultado TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS ix_th_nombre ON tareas_historial(nombre, id);
"""

TURNO_MINUTOS = 30          # si un proceso muere a mitad de tarea, otro la retoma pasado este tiempo
VUELTA_S = 20


def init(con):
    con.executescript(SCHEMA)
    con.commit()


@dataclass
class Tarea:
    nombre: str
    titulo: str
    toca: Callable            # (con, ultima_ejecucion: datetime | None, ahora: datetime) -> bool
    ejecutar: Callable        # (con) -> str   (resumen legible)


_TAREAS: dict[str, Tarea] = {}


def registrar(tarea: Tarea) -> None:
    _TAREAS[tarea.nombre] = tarea


def cada_minutos(clave_ajuste: str, por_defecto: int, clave_activa: str | None = None) -> Callable:
    """Regla «cada N minutos», configurable en ajustes; con `clave_activa`, solo si ese ajuste vale «1»."""
    def toca(con, ultima, ahora):
        if clave_activa and db.get_setting(con, clave_activa, "0") != "1":
            return False
        n = int(db.get_setting(con, clave_ajuste, str(por_defecto)) or por_defecto)
        return ultima is None or ahora - ultima >= timedelta(minutes=max(1, n))
    return toca


def _estado(con, nombre: str) -> dict:
    r = db.one(con, "SELECT * FROM tareas_programadas WHERE nombre=?", (nombre,))
    if not r:
        con.execute("INSERT OR IGNORE INTO tareas_programadas (nombre) VALUES (?)", (nombre,))
        con.commit()
        r = db.one(con, "SELECT * FROM tareas_programadas WHERE nombre=?", (nombre,))
    return r


def _reservar(con, nombre: str, turno: str, ahora: datetime) -> bool:
    cur = con.execute("""UPDATE tareas_programadas SET turno=?, turno_hasta=? WHERE nombre=?
                         AND (turno IS NULL OR turno_hasta IS NULL OR turno_hasta < ?)""",
                      (turno, (ahora + timedelta(minutes=TURNO_MINUTOS)).isoformat(timespec="seconds"), nombre,
                       ahora.isoformat(timespec="seconds")))
    con.commit()
    return cur.rowcount == 1


def ejecutar_ahora(con, nombre: str) -> dict:
    """Ejecuta una tarea en este mismo hilo (botón «Ejecutar ahora» y pruebas). Respeta el turno."""
    t = _TAREAS[nombre]
    _estado(con, nombre)
    turno = uuid.uuid4().hex
    inicio = datetime.now()
    if not _reservar(con, nombre, turno, inicio):
        return {"ok": False, "resultado": None, "error": "La tarea ya se está ejecutando en este momento."}
    ok, res, err = True, "", None
    try:
        res = t.ejecutar(con) or ""
    except Exception as e:  # noqa: BLE001
        con.rollback()
        ok, err = False, f"{type(e).__name__}: {e}"[:1000]
        traceback.print_exc()
    fin = datetime.now()
    con.execute("""UPDATE tareas_programadas SET ultima_ejecucion=?, ultimo_ok=CASE WHEN ? THEN ? ELSE ultimo_ok END,
                   ultimo_error=?, ultimo_resultado=?, duracion_s=?, turno=NULL, turno_hasta=NULL, forzar=0 WHERE nombre=? AND turno=?""",
                (inicio.isoformat(timespec="seconds"), int(ok), fin.isoformat(timespec="seconds"), err, str(res)[:1000],
                 round((fin - inicio).total_seconds(), 1), nombre, turno))
    con.execute("INSERT INTO tareas_historial (nombre, inicio, fin, ok, resultado, error) VALUES (?,?,?,?,?,?)",
                (nombre, inicio.isoformat(timespec="seconds"), fin.isoformat(timespec="seconds"), int(ok), str(res)[:1000], err))
    con.execute("DELETE FROM tareas_historial WHERE nombre=? AND id NOT IN (SELECT id FROM tareas_historial WHERE nombre=? ORDER BY id DESC LIMIT 200)",
                (nombre, nombre))
    con.commit()
    return {"ok": ok, "resultado": res, "error": err}


def pedir_ejecucion(con, nombre: str) -> None:
    """Marca la tarea para que el hilo de fondo la ejecute en la siguiente vuelta (no bloquea la pantalla)."""
    _estado(con, nombre)
    con.execute("UPDATE tareas_programadas SET forzar=1 WHERE nombre=?", (nombre,))
    con.commit()
    _evento.set()


def estado(con) -> list[dict]:
    out = []
    for n, t in _TAREAS.items():
        r = _estado(con, n)
        out.append({"nombre": n, "titulo": t.titulo, **{k: r.get(k) for k in ("ultima_ejecucion", "ultimo_ok", "ultimo_error",
                                                                                "ultimo_resultado", "duracion_s", "turno_hasta")}})
    return out


def historial(con, nombre: str, n: int = 20) -> list[dict]:
    return db.rows(con, "SELECT * FROM tareas_historial WHERE nombre=? ORDER BY id DESC LIMIT ?", (nombre, n))


def pendientes(con, ahora: datetime | None = None) -> list[str]:
    ahora = ahora or datetime.now()
    out = []
    for n, t in _TAREAS.items():
        r = _estado(con, n)
        ultima = datetime.fromisoformat(r["ultima_ejecucion"]) if r.get("ultima_ejecucion") else None
        try:
            if r.get("forzar") or t.toca(con, ultima, ahora):
                out.append(n)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    return out


# ============================================================================ hilo de fondo
_evento = threading.Event()
_hilo = None
_lock = threading.Lock()


def registrar_todas() -> None:
    """Cada módulo declara sus tareas. Se importa aquí para evitar ciclos."""
    from . import buzon, respaldo, correo, sis, actas_audio
    for m in (buzon, respaldo, correo, sis, actas_audio):
        for t in m.tareas():
            registrar(t)


def _bucle():
    con = db.connect()
    init(con)
    while True:
        try:
            for n in pendientes(con):
                ejecutar_ahora(con, n)
        except Exception:  # noqa: BLE001 - el hilo nunca debe morir
            traceback.print_exc()
            try:
                con.rollback()
            except Exception:
                pass
        _evento.wait(VUELTA_S)
        _evento.clear()


def arrancar() -> None:
    global _hilo
    with _lock:
        registrar_todas()
        if _hilo is None or not _hilo.is_alive():
            _hilo = threading.Thread(target=_bucle, name="tareas-automaticas", daemon=True)
            _hilo.start()
