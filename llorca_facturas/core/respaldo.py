"""
COPIA DE SEGURIDAD AUTOMÁTICA.

Si falla el disco del servidor se pierde la carpeta `data` (base de datos + PDFs). Esta tarea lo evita:

- Cada día a la hora fijada (y, opcionalmente, cada N horas) se copia a uno o varios destinos (otro disco, una unidad de
  red o NAS `\\\\servidor\\copias`, una carpeta sincronizada con la nube…):
  * `bd/llorca_AAAAMMDD_HHMMSS.db.gz`: foto CONSISTENTE de la base de datos (API de copia de SQLite: vale aunque la
    aplicación esté en uso), comprimida;
  * `archivos/`: espejo incremental de PDFs, justificantes, planos, audios… (solo se copia lo nuevo o cambiado, y nunca
    se borra nada del espejo).
- Cada copia se VERIFICA: se descomprime, se abre, `PRAGMA integrity_check` y se comparan los recuentos con el original.
  Una copia que no se puede restaurar no cuenta como copia.
- Rotación abuelo-padre-hijo: se conservan las últimas N diarias, la primera de cada una de las últimas N semanas y la
  primera de cada uno de los últimos N meses.
- Aviso en el centro de alertas si la última copia correcta tiene más de 26 horas, si un destino falla o si el destino
  está en el mismo disco que los datos (eso no protege de un fallo de disco).
- Restauración: `python restaurar_copia.py <copia.db.gz>` (con la aplicación parada). Desde Configuración se puede hacer
  un simulacro de restauración sin tocar nada.

La clave local que cifra las contraseñas guardadas (`data/.clave_secretos`) NO se copia a propósito: si se restaura en
otro equipo, basta con volver a introducir las contraseñas del buzón, del correo y de SIS.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS respaldos (
    id INTEGER PRIMARY KEY, inicio TEXT, fin TEXT, destino TEXT, archivo TEXT, bytes INTEGER, sha256 TEXT,
    ok INTEGER DEFAULT 0, verificado INTEGER DEFAULT 0, archivos_copiados INTEGER DEFAULT 0, archivos_bytes INTEGER DEFAULT 0,
    borradas INTEGER DEFAULT 0, detalle TEXT, error TEXT, usuario TEXT);
"""
EXCLUIR = {DB_PATH.name, DB_PATH.name + "-wal", DB_PATH.name + "-shm", DB_PATH.name + "-journal", ".clave_secretos"}
EXCLUIR_CARPETAS = {"knowledge", "__pycache__", "tmp"}          # índice semántico: se reconstruye desde Configuración
TABLAS_CONTROL = ("documentos", "lineas", "certificaciones", "cert_lineas", "facturas_emitidas", "auditoria")


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def config(con) -> dict:
    g = lambda k, d="": db.get_setting(con, "respaldo_" + k, d)  # noqa: E731
    destinos = [x.strip() for x in g("destinos", str(DATA_DIR.parent / "copias_seguridad")).replace("\n", ";").split(";") if x.strip()]
    return {"activo": g("activo", "1") == "1", "destinos": destinos, "hora": g("hora", "21:30") or "21:30",
            "cada_horas": int(g("cada_horas", "0") or 0), "diarias": int(g("diarias", "14") or 14),
            "semanales": int(g("semanales", "8") or 8), "mensuales": int(g("mensuales", "12") or 12),
            "archivos": g("archivos", "1") == "1"}


# ============================================================================ piezas
def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _recuentos(c: sqlite3.Connection) -> dict:
    out = {}
    for t in TABLAS_CONTROL:
        try:
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            pass
    return out


def foto_bd(con, destino_gz: Path) -> dict:
    """Copia consistente de la base de datos, comprimida. Devuelve recuentos del original en el mismo instante."""
    destino_gz.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="llorca_copia_"))
    try:
        tmp = tmp_dir / "copia.db"
        dst = sqlite3.connect(tmp)
        con.backup(dst)
        dst.close()
        dst = sqlite3.connect(tmp)
        recuentos = _recuentos(dst)
        dst.close()
        parcial = destino_gz.with_suffix(destino_gz.suffix + ".parcial")
        with open(tmp, "rb") as fi, gzip.open(parcial, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        os.replace(parcial, destino_gz)            # nunca queda a medias con el nombre definitivo
        return recuentos
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def verificar(archivo_gz: Path, esperados: dict | None = None) -> tuple[bool, str]:
    """Descomprime en temporal, comprueba la integridad y (si se dan) los recuentos."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="llorca_verif_"))
    try:
        tmp = tmp_dir / "v.db"
        with gzip.open(archivo_gz, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        c = sqlite3.connect(tmp)
        r = c.execute("PRAGMA integrity_check").fetchone()[0]
        rec = _recuentos(c)
        c.close()
        if r != "ok":
            return False, f"integridad: {r}"
        if esperados:
            dif = {k: (v, rec.get(k)) for k, v in esperados.items() if rec.get(k) != v}
            if dif:
                return False, f"recuentos distintos: {dif}"
        return True, ", ".join(f"{k} {v}" for k, v in rec.items())
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def espejo_archivos(origen: Path, destino: Path) -> tuple[int, int]:
    """Copia incremental: solo lo que no existe o ha cambiado de tamaño/fecha. No borra nada del destino."""
    n = b = 0
    for raiz, carpetas, archivos in os.walk(origen):
        carpetas[:] = [c for c in carpetas if c not in EXCLUIR_CARPETAS and not c.startswith(".")]
        rel = Path(raiz).relative_to(origen)
        for a in archivos:
            if (rel == Path(".") and a in EXCLUIR) or a.endswith((".parcial", ".tmp")):
                continue
            src = Path(raiz) / a
            dst = destino / rel / a
            try:
                st = src.stat()
                if dst.exists():
                    sd = dst.stat()
                    if sd.st_size == st.st_size and int(sd.st_mtime) >= int(st.st_mtime):
                        continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".parcial")
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
                n += 1
                b += st.st_size
            except FileNotFoundError:
                continue                         # se borró mientras se copiaba
    return n, b


def _fecha_de(nombre: str) -> datetime | None:
    try:
        return datetime.strptime(nombre.split("_", 1)[1].split(".")[0], "%Y%m%d_%H%M%S")
    except Exception:
        return None


def a_conservar(fechas: list[datetime], diarias: int, semanales: int, mensuales: int) -> set[datetime]:
    """Rotación abuelo-padre-hijo sobre las fechas de las copias existentes."""
    fechas = sorted(fechas, reverse=True)
    keep = set()
    dias, semanas, meses = {}, {}, {}
    for f in sorted(fechas):                       # la PRIMERA copia de cada día/semana/mes
        dias.setdefault(f.date(), f)
        semanas.setdefault(tuple(f.isocalendar())[:2], f)
        meses.setdefault((f.year, f.month), f)
    # de cada día se conserva la ÚLTIMA (la más completa)
    ult_dia = {}
    for f in fechas:
        ult_dia.setdefault(f.date(), f)
    keep |= {ult_dia[d] for d in sorted(ult_dia, reverse=True)[:diarias]}
    keep |= {semanas[k] for k in sorted(semanas, reverse=True)[:semanales]}
    keep |= {meses[k] for k in sorted(meses, reverse=True)[:mensuales]}
    if fechas:
        keep.add(fechas[0])                        # la más reciente, siempre
    return keep


def rotar(carpeta_bd: Path, cfg: dict) -> int:
    copias = {}
    for p in carpeta_bd.glob("llorca_*.db.gz"):
        f = _fecha_de(p.name)
        if f:
            copias[f] = p
    keep = a_conservar(list(copias), cfg["diarias"], cfg["semanales"], cfg["mensuales"])
    n = 0
    for f, p in copias.items():
        if f not in keep:
            p.unlink(missing_ok=True)
            Path(str(p) + ".json").unlink(missing_ok=True)
            n += 1
    return n


def mismo_disco(a: Path, b: Path) -> bool:
    try:
        b.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            da, dbb = os.path.splitdrive(str(a.resolve()))[0].upper(), os.path.splitdrive(str(b.resolve()))[0].upper()
            return bool(da) and da == dbb and not str(b).startswith("\\\\")
        return os.stat(a).st_dev == os.stat(b).st_dev
    except Exception:
        return False


# ============================================================================ ejecución
def copiar(con, cfg: dict | None = None, usuario: str = "copia automática") -> str:
    cfg = cfg or config(con)
    if not cfg["destinos"]:
        raise ValueError("No hay ningún destino de copia configurado.")
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    resumen, errores = [], []
    for dest in cfg["destinos"]:
        inicio = db.now_iso()
        raiz = Path(dest)
        cur = con.execute("INSERT INTO respaldos (inicio, destino, usuario) VALUES (?,?,?)", (inicio, dest, usuario))
        rid = cur.lastrowid
        con.commit()
        try:
            try:
                dentro = raiz.resolve().is_relative_to(Path(DATA_DIR).resolve())
            except (OSError, ValueError):
                dentro = False
            if dentro:
                raise ValueError("El destino está dentro de la carpeta de datos: la copia se copiaría a sí misma. Elija otra carpeta.")
            raiz.mkdir(parents=True, exist_ok=True)
            gz = raiz / "bd" / f"llorca_{sello}.db.gz"
            esperados = foto_bd(con, gz)
            ok, det = verificar(gz, esperados)
            if not ok:
                raise RuntimeError(f"La copia no supera la verificación: {det}")
            sha = _sha256(gz)
            Path(str(gz) + ".json").write_text(json.dumps({"archivo": gz.name, "sha256": sha, "creada": inicio, "recuentos": esperados,
                                                           "origen": str(DB_PATH)}, ensure_ascii=False, indent=1), encoding="utf-8")
            n_arch = b_arch = 0
            if cfg["archivos"]:
                n_arch, b_arch = espejo_archivos(DATA_DIR, raiz / "archivos")
            borradas = rotar(raiz / "bd", cfg)
            con.execute("""UPDATE respaldos SET fin=?, archivo=?, bytes=?, sha256=?, ok=1, verificado=1, archivos_copiados=?, archivos_bytes=?,
                           borradas=?, detalle=? WHERE id=?""",
                        (db.now_iso(), str(gz), gz.stat().st_size, sha, n_arch, b_arch, borradas, det, rid))
            resumen.append(f"{dest}: {gz.stat().st_size / 1e6:.1f} MB verificada, {n_arch} archivo(s) nuevos")
        except Exception as e:  # noqa: BLE001 - un destino caído no impide copiar en los demás
            con.execute("UPDATE respaldos SET fin=?, ok=0, error=? WHERE id=?", (db.now_iso(), f"{type(e).__name__}: {e}"[:800], rid))
            errores.append(f"{dest}: {e}")
        con.commit()
    db.audit(con, usuario, "copia_seguridad", "respaldo", None, {"correctas": resumen, "errores": errores})
    con.commit()
    if errores and not resumen:
        raise RuntimeError("Ninguna copia correcta. " + " | ".join(errores))
    return " · ".join(resumen + [f"ERROR {e}" for e in errores])


def ultima_ok(con) -> dict | None:
    return db.one(con, "SELECT * FROM respaldos WHERE ok=1 ORDER BY id DESC LIMIT 1")


def avisos(con) -> list[str]:
    cfg = config(con)
    out = []
    if not cfg["activo"]:
        return ["La copia de seguridad automática está DESACTIVADA."]
    u = ultima_ok(con)
    if not u:
        out.append("Todavía no hay ninguna copia de seguridad correcta.")
    elif datetime.fromisoformat(u["fin"]) < datetime.now() - timedelta(hours=26 + cfg["cada_horas"]):
        out.append(f"La última copia correcta es del {u['fin'][:16].replace('T', ' ')}: hace más de un día.")
    for d in cfg["destinos"]:
        ult = db.one(con, "SELECT ok, error FROM respaldos WHERE destino=? ORDER BY id DESC LIMIT 1", (d,))
        if ult and not ult["ok"]:
            out.append(f"La última copia en «{d}» falló: {ult['error']}")
    if cfg["destinos"] and all(mismo_disco(DATA_DIR, Path(d)) for d in cfg["destinos"]):
        out.append("Todas las copias están en el MISMO disco que los datos: no protegen de un fallo de disco. "
                   "Añada un destino en otro disco, NAS o carpeta de red.")
    return out


def toca(con, ultima, ahora) -> bool:
    cfg = config(con)
    if not cfg["activo"]:
        return False
    if ultima is None and not ultima_ok(con):
        return True                                    # primera vez: copia inmediata
    try:
        hh, mm = (int(x) for x in cfg["hora"].split(":"))
    except ValueError:
        hh, mm = 21, 30
    hito = ahora.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if ahora >= hito and (ultima is None or ultima < hito):
        return True
    return bool(cfg["cada_horas"] and ultima and ahora - ultima >= timedelta(hours=cfg["cada_horas"]))


def tareas():
    from .planificador import Tarea
    return [Tarea("respaldo", "Copia de seguridad automática", toca, copiar)]


# ============================================================================ restauración
def listar_copias(destino: str) -> list[dict]:
    out = []
    for p in sorted((Path(destino) / "bd").glob("llorca_*.db.gz"), reverse=True):
        meta = {}
        try:
            meta = json.loads(Path(str(p) + ".json").read_text(encoding="utf-8"))
        except Exception:
            pass
        out.append({"archivo": str(p), "fecha": _fecha_de(p.name), "mb": round(p.stat().st_size / 1e6, 2), "sha256": meta.get("sha256"),
                    "recuentos": meta.get("recuentos")})
    return out


def simulacro(archivo_gz: str) -> tuple[bool, str]:
    """Restaura en temporal y comprueba: demuestra que la copia sirve, sin tocar los datos en uso."""
    p = Path(archivo_gz)
    meta = {}
    try:
        meta = json.loads(Path(str(p) + ".json").read_text(encoding="utf-8"))
    except Exception:
        pass
    if meta.get("sha256") and meta["sha256"] != _sha256(p):
        return False, "La huella SHA-256 no coincide: el archivo de copia está dañado."
    return verificar(p, meta.get("recuentos"))


def restaurar(archivo_gz: str, destino_db: Path = DB_PATH) -> Path:
    """Sustituye la base de datos por la copia. USAR CON LA APLICACIÓN PARADA. La base actual se conserva al lado."""
    ok, det = simulacro(archivo_gz)
    if not ok:
        raise RuntimeError(f"La copia no es válida: {det}")
    destino_db = Path(destino_db)
    previa = None
    if destino_db.exists():
        previa = destino_db.with_name(destino_db.stem + f".antes_de_restaurar_{datetime.now():%Y%m%d_%H%M%S}.db")
        src = sqlite3.connect(destino_db)
        dst = sqlite3.connect(previa)
        src.backup(dst)
        dst.close()
        src.close()
    tmp = destino_db.with_suffix(".restaurando")
    with gzip.open(archivo_gz, "rb") as fi, open(tmp, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    for suf in ("-wal", "-shm"):
        Path(str(destino_db) + suf).unlink(missing_ok=True)
    os.replace(tmp, destino_db)
    return previa or destino_db
