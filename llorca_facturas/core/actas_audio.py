"""
ACTAS DESDE EL AUDIO DE LA REUNIÓN (transcripción 100 % local).

- Se sube la grabación (m4a, mp3, wav, ogg, opus, webm, mp4…) de la reunión de obra.
- Un proceso de fondo la transcribe EN EL PROPIO EQUIPO con Whisper (faster-whisper, CPU, cuantizado int8; o
  openai-whisper si es el que está instalado). El audio no sale de la oficina.
- De la transcripción se proponen compromisos, decisiones y posibles extras con las mismas reglas que el diario de obra
  (responsable, fecha límite e importe cuando se mencionan). Si hay IA local (Ollama), además redacta un resumen en
  formato de acta; si no, el acta lleva la transcripción con marcas de tiempo.
- Nada se guarda en el diario sin revisión: la persona corrige, elige qué compromisos entran y crea la reunión con un
  clic. El acta interna y la de cliente se imprimen desde el diario como siempre.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from pathlib import Path

from . import db
from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS actas_audio (
    id INTEGER PRIMARY KEY, obra_id INTEGER REFERENCES obras(id) ON DELETE SET NULL, titulo TEXT, fecha TEXT, asistentes TEXT,
    archivo TEXT, nombre_archivo TEXT, sha256 TEXT UNIQUE, estado TEXT NOT NULL DEFAULT 'pendiente', transcripcion TEXT, segmentos TEXT,
    resumen TEXT, duracion_s REAL, segundos_proceso REAL, modelo TEXT, idioma TEXT, error TEXT, reunion_id INTEGER,
    creado_por TEXT, creado_en TEXT, terminado_en TEXT);
"""
EXT_AUDIO = ["m4a", "mp3", "wav", "ogg", "opus", "webm", "mp4", "aac", "flac", "wma", "amr", "3gp"]
ESTADOS = {"pendiente": "En cola", "transcribiendo": "Transcribiendo…", "transcrita": "Transcrita (revisar)", "error": "Error",
           "procesada": "Pasada al diario"}
MAX_MB = 500


def init(con):
    con.executescript(SCHEMA)
    con.execute("UPDATE actas_audio SET estado='pendiente' WHERE estado='transcribiendo'")      # interrumpidas por un reinicio
    con.commit()


def carpeta() -> Path:
    p = DATA_DIR / "actas_audio"
    p.mkdir(parents=True, exist_ok=True)
    return p


def motor_disponible() -> tuple[str | None, str]:
    try:
        import faster_whisper  # noqa: F401  (solo se comprueba que está instalado)
        return "faster-whisper", "faster-whisper instalado"
    except ImportError:
        pass
    try:
        import whisper  # noqa: F401
        return "openai-whisper", "openai-whisper instalado"
    except ImportError:
        pass
    return None, ("No hay motor de transcripción local. Instálelo una vez en el entorno: "
                  "`pip install faster-whisper` (instalar.bat lo intenta automáticamente).")


def subir(con, obra_id: int | None, titulo: str, fecha: str, asistentes: str, nombre: str, data: bytes, usuario: str) -> int:
    ext = Path(nombre).suffix.lower().lstrip(".")
    if ext not in EXT_AUDIO:
        raise ValueError(f"Formato de audio no admitido ({ext}). Admitidos: {', '.join(EXT_AUDIO)}.")
    if not data or len(data) < 1000:
        raise ValueError("Archivo vacío.")
    if len(data) > MAX_MB * 1024 * 1024:
        raise ValueError(f"El audio supera {MAX_MB} MB.")
    h = hashlib.sha256(data).hexdigest()
    ya = db.one(con, "SELECT id, titulo FROM actas_audio WHERE sha256=?", (h,))
    if ya:
        raise ValueError(f"Ese audio ya se subió («{ya['titulo']}», #{ya['id']}).")
    ruta = carpeta() / f"{h[:16]}.{ext}"
    ruta.write_bytes(data)
    with db.tx(con):
        cur = con.execute("""INSERT INTO actas_audio (obra_id, titulo, fecha, asistentes, archivo, nombre_archivo, sha256, estado, creado_por, creado_en)
                             VALUES (?,?,?,?,?,?,?,'pendiente',?,?)""", (obra_id, titulo.strip() or "Reunión de obra", fecha, asistentes, str(ruta), nombre, h,
                                                                        usuario, db.now_iso()))
        db.audit(con, usuario, "acta_audio_subida", "acta_audio", cur.lastrowid, {"archivo": nombre, "mb": round(len(data) / 1e6, 1)})
    try:
        from . import planificador
        planificador.pedir_ejecucion(con, "actas")
    except Exception:
        pass
    return cur.lastrowid


def _fmt_t(s: float) -> str:
    s = int(s)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def transcribir_archivo(ruta: str, modelo: str = "small", idioma: str = "es") -> dict:
    motor, msg = motor_disponible()
    if not motor:
        raise RuntimeError(msg)
    if motor == "faster-whisper":
        from faster_whisper import WhisperModel
        m = WhisperModel(modelo, device="cpu", compute_type="int8")
        segs, info = m.transcribe(ruta, language=idioma or None, vad_filter=True, beam_size=5,
                                  initial_prompt="Reunión de obra de construcción: certificación, subcontrata, jefe de obra, dirección facultativa, planos, partidas.")
        segmentos = [{"inicio": s.start, "fin": s.end, "texto": s.text.strip()} for s in segs]
        dur = getattr(info, "duration", None)
        lang = getattr(info, "language", idioma)
    else:
        import whisper
        m = whisper.load_model(modelo)
        r = m.transcribe(ruta, language=idioma or None, fp16=False)
        segmentos = [{"inicio": s["start"], "fin": s["end"], "texto": s["text"].strip()} for s in r.get("segments", [])]
        dur = segmentos[-1]["fin"] if segmentos else None
        lang = r.get("language", idioma)
    return {"segmentos": segmentos, "texto": " ".join(s["texto"] for s in segmentos).strip(), "duracion": dur, "idioma": lang,
            "motor": f"{motor} ({modelo})"}


def resumir(con, texto: str) -> str | None:
    """Resumen en formato de acta con la IA local, si está disponible. Nunca inventa: se le pide ceñirse a la transcripción."""
    try:
        from . import lector
        from .ollama_cliente import chat, elegir_modelo_razonamiento
        cfg = lector.config(con)
        modelo = elegir_modelo_razonamiento(cfg["ollama_url"], cfg["ollama_razonamiento_modelo"])
        if not modelo:
            return None
        msg = chat(cfg["ollama_url"], modelo, [
            {"role": "system", "content": "Eres el secretario de una reunión de obra. Redacta un acta breve en español a partir de la transcripción. "
                                          "Secciones: Asuntos tratados, Decisiones, Compromisos (quién, qué, cuándo), Posibles extras o modificaciones. "
                                          "No inventes nada que no esté en la transcripción; si algo no queda claro, indícalo."},
            {"role": "user", "content": texto[:24000]}], opciones={"num_ctx": 8192, "num_predict": 1200}, timeout=900)
        return (msg.get("content") or "").strip() or None
    except Exception:  # noqa: BLE001 - sin IA local el acta sigue con la transcripción
        return None


def procesar_pendiente(con) -> str:
    a = db.one(con, "SELECT * FROM actas_audio WHERE estado='pendiente' ORDER BY id LIMIT 1")
    if not a:
        return "Nada que transcribir."
    motor, msg = motor_disponible()
    if not motor:
        con.execute("UPDATE actas_audio SET estado='error', error=? WHERE id=?", (msg, a["id"]))
        con.commit()
        return msg
    con.execute("UPDATE actas_audio SET estado='transcribiendo', error=NULL WHERE id=?", (a["id"],))
    con.commit()
    t0 = time.time()
    try:
        r = transcribir_archivo(a["archivo"], db.get_setting(con, "whisper_modelo", "small"), db.get_setting(con, "whisper_idioma", "es"))
        resumen = resumir(con, r["texto"]) if db.get_setting(con, "actas_resumen_ia", "1") == "1" and r["texto"] else None
        con.execute("""UPDATE actas_audio SET estado='transcrita', transcripcion=?, segmentos=?, resumen=?, duracion_s=?, segundos_proceso=?, modelo=?,
                       idioma=?, terminado_en=? WHERE id=?""",
                    (r["texto"], json.dumps(r["segmentos"], ensure_ascii=False), resumen, r["duracion"], round(time.time() - t0, 1), r["motor"],
                     r["idioma"], db.now_iso(), a["id"]))
        con.commit()
        return f"Transcrita «{a['titulo']}» ({_fmt_t(r['duracion'] or 0)} de audio en {_fmt_t(time.time() - t0)})"
    except Exception as e:  # noqa: BLE001
        con.execute("UPDATE actas_audio SET estado='error', error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:500], a["id"]))
        con.commit()
        raise


def texto_con_tiempos(acta: dict) -> str:
    segs = json.loads(acta.get("segmentos") or "[]")
    return "\n".join(f"[{_fmt_t(s['inicio'])}] {s['texto']}" for s in segs) or (acta.get("transcripcion") or "")


def propuestas(acta: dict) -> list[dict]:
    from .gobierno import analizar_notas
    ref = date.fromisoformat(acta["fecha"]) if acta.get("fecha") else date.today()
    segs = json.loads(acta.get("segmentos") or "[]")
    texto = "\n".join(s["texto"] for s in segs) if segs else (acta.get("transcripcion") or "")
    return analizar_notas(texto, ref)


def crear_reunion(con, acta_id: int, obra_id: int, fecha: str, titulo: str, asistentes: str, notas: str, compromisos: list[dict],
                  usuario: str) -> int:
    """Pasa el acta revisada al diario de obra (reunión + compromisos elegidos)."""
    from .money import parse_amount, to_cents
    a = db.one(con, "SELECT * FROM actas_audio WHERE id=?", (acta_id,))
    if a["estado"] == "procesada":
        raise ValueError("Esta acta ya se pasó al diario.")
    if not obra_id:
        raise ValueError("Indique la obra.")
    with db.tx(con):
        cur = con.execute("INSERT INTO obra_reuniones (obra_id, fecha, titulo, asistentes, notas, creado_por, creado_en) VALUES (?,?,?,?,?,?,?)",
                          (obra_id, fecha, titulo, asistentes, notas, usuario, db.now_iso()))
        rid = cur.lastrowid
        for c in compromisos:
            if not str(c.get("descripcion") or "").strip():
                continue
            imp = str(c.get("importe") or "").strip()
            con.execute("""INSERT INTO obra_compromisos (obra_id, reunion_id, tipo, descripcion, responsable, fecha_limite, importe_estimado_cents,
                           creado_por, creado_en) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (obra_id, rid, c.get("tipo") or "compromiso", c["descripcion"], c.get("responsable") or None, c.get("fecha_limite") or None,
                         to_cents(parse_amount(imp)) if imp else None, usuario, db.now_iso()))
        con.execute("UPDATE actas_audio SET estado='procesada', reunion_id=?, obra_id=? WHERE id=?", (rid, obra_id, acta_id))
        db.audit(con, usuario, "acta_audio_a_diario", "reunion", rid, {"acta": acta_id, "compromisos": len(compromisos)})
    return rid


def reintentar(con, acta_id: int, usuario: str) -> None:
    with db.tx(con):
        con.execute("UPDATE actas_audio SET estado='pendiente', error=NULL WHERE id=? AND estado='error'", (acta_id,))
        db.audit(con, usuario, "acta_audio_reintentar", "acta_audio", acta_id, None)


def tareas():
    from .planificador import Tarea

    def toca(con, ultima, ahora):
        return bool(db.one(con, "SELECT 1 x FROM actas_audio WHERE estado='pendiente' LIMIT 1")) and \
            (ultima is None or ahora - ultima >= timedelta(seconds=30))
    return [Tarea("actas", "Transcripción de actas de reunión", toca, procesar_pendiente)]
