"""Utilidades PDF: hash, texto, renderizado de páginas y extracción de lotes ZIP."""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path

import pdfplumber

from .config import PDF_DIR


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_pdf(data: bytes) -> tuple[str, Path]:
    h = sha256(data)
    path = PDF_DIR / f"{h}.pdf"
    if not path.exists():
        path.write_bytes(data)
    return h, path


def read_text(data: bytes, max_pages: int = 60) -> tuple[str, int]:
    """Devuelve (texto, nº páginas). Texto vacío = PDF escaneado (sin capa de texto)."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            n = len(pdf.pages)
            parts = []
            for p in pdf.pages[:max_pages]:
                parts.append(p.extract_text(layout=False) or "")
            return "\n".join(parts), n
    except Exception:
        return "", 0


def has_text_layer(text: str) -> bool:
    return len(re.findall(r"\w+", text or "")) >= 25


def render_pages(data: bytes, max_pages: int = 5, scale: float = 1.6):
    """Imágenes PIL de las primeras páginas (para el visor de revisión)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    out = []
    for i in range(min(len(pdf), max_pages)):
        out.append(pdf[i].render(scale=scale).to_pil())
    return out


EXT_IMAGEN = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif")
EXT_ADMITIDAS = ["pdf", "zip", "jpg", "jpeg", "png", "tif", "tiff", "bmp", "webp", "eml", "msg"]


def imagen_a_pdf(data: bytes) -> bytes:
    """Foto o escaneo (JPG, PNG, TIFF multipágina…) -> PDF, para que siga el mismo circuito (OCR/visión)."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    paginas = []
    for i in range(getattr(im, "n_frames", 1)):
        im.seek(i)
        p = ImageOps.exif_transpose(im.copy()).convert("RGB")      # respeta la orientación de las fotos de móvil
        if max(p.size) > 3000:
            r = 3000 / max(p.size)
            p = p.resize((int(p.width * r), int(p.height * r)))
        paginas.append(p)
    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=200)
    return buf.getvalue()


def _adjuntos_correo(name: str, data: bytes):
    """Adjuntos de un correo (.eml estándar o .msg de Outlook)."""
    if name.lower().endswith(".eml"):
        import email
        from email import policy
        msg = email.message_from_bytes(data, policy=policy.default)
        for part in msg.iter_attachments():
            fn = part.get_filename()
            if fn:
                yield fn, part.get_payload(decode=True)
    else:
        try:
            import extract_msg
        except ImportError:
            return
        m = extract_msg.Message(io.BytesIO(data))
        for a in m.attachments:
            fn = getattr(a, "longFilename", None) or getattr(a, "shortFilename", None)
            if fn and isinstance(a.data, bytes):
                yield fn, a.data


def iter_pdfs_from_upload(name: str, data: bytes, _nivel: int = 0):
    """PDF, imágenes (se convierten a PDF), ZIP (con subcarpetas y ZIP dentro de ZIP) y correos con adjuntos.
    Devuelve (nombre, bytes_pdf)."""
    n = name.lower()
    if n.endswith(".zip") and _nivel < 3:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir() or "__MACOSX" in info.filename or Path(info.filename).name.startswith("."):
                    continue
                yield from iter_pdfs_from_upload(Path(info.filename).name, z.read(info), _nivel + 1)
    elif n.endswith((".eml", ".msg")) and _nivel < 3:
        for fn, d in _adjuntos_correo(name, data):
            yield from iter_pdfs_from_upload(fn, d, _nivel + 1)
    elif n.endswith(".pdf") or data[:5] == b"%PDF-":
        yield name, data
    elif n.endswith(EXT_IMAGEN):
        try:
            from PIL import Image
            w, h = Image.open(io.BytesIO(data)).size
            if min(w, h) < 500:          # logotipos y firmas de correo: no son documentos
                return
            yield Path(name).stem + ".pdf", imagen_a_pdf(data)
        except Exception:
            return


def normalize_for_search(text: str) -> str:
    """Texto sin espacios internos en números para buscar importes literalmente."""
    t = (text or "").replace("\u2212", "-").replace("\u200b", "").replace("\xa0", " ")
    # Une '1 234,56' -> '1234,56' solo cuando son grupos de miles
    t = re.sub(r"(?<=\d) (?=\d{3}(?:[.,]\d{1,2})?\b)", "", t)
    # Algunos PDFs se extraen con espacios espurios dentro del número: '1 .892,36', '9 4,62'.
    # Se añade una segunda versión 'compactada' (sin espacios entre dígitos y separadores).
    compact = re.sub(r"(?<=[\d.,]) (?=[\d.,])", "", t)
    return t + "\n" + compact
