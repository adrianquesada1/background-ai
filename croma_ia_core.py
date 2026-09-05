"""
croma_ia_core.py
-----------------
Recorte de fondo mediante un modelo de IA real de segmentación (rembg /
U2-Net), no un simple umbral de color — funciona con cualquier fondo, no
solo verde, y no necesita calibración por foto.

La primera vez que se usa, descarga el modelo (~176 MB) una sola vez.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageOps, ImageFilter, ImageDraw, ImageEnhance
from rembg import remove, new_session


@dataclass
class ParametrosIA:
    rotar_a_vertical: bool = True
    rotacion_manual: int = 0            # 0, 90, 180, 270 (grados, sentido horario)
    recortar_ajustado: bool = True
    margen_recorte_pct: float = 0.32    # más aire alrededor de la prenda
    añadir_sombra: bool = True
    intensidad_sombra: float = 0.22   # 0=sin sombra, 1=sombra muy marcada
    fondo_con_profundidad: bool = True  # degradado sutil tipo papel de estudio
    sombra_contorno: bool = True        # sombra que seguir la silueta (look "elevado", no pegatina)
    intensidad_contorno: float = 0.35
    tamano_fijo: Optional[Tuple[int, int]] = None  # (ancho, alto): mismo lienzo para toda la tanda
    mejorar_color: bool = True          # contraste/saturación/nitidez sutiles, tipo post-proceso de estudio
    intensidad_contraste: float = 1.06
    intensidad_saturacion: float = 1.08
    intensidad_nitidez: float = 1.15


_SESSION_CACHE = {}


def obtener_session(modelo: str = "u2net"):
    """Carga (o reutiliza) la sesión del modelo de IA. Pesado -> cachear
    en el llamador (p.ej. st.cache_resource en la app de Streamlit)."""
    if modelo not in _SESSION_CACHE:
        _SESSION_CACHE[modelo] = new_session(modelo)
    return _SESSION_CACHE[modelo]


def _mejorar_color(rgba: Image.Image, contraste: float, saturacion: float, nitidez: float) -> Image.Image:
    """
    Ajuste sutil de post-proceso, como haría un fotógrafo de producto:
    un pelín de contraste, saturación y nitidez. Nada de estirar el
    histograma por canal (probado y descartado: en telas oscuras casi
    monocromas metía un tono grisáceo/plateado raro, se veía peor que
    el original).
    """
    r, g, b, a = rgba.split()
    rgb_img = Image.merge("RGB", (r, g, b))
    rgb_img = ImageEnhance.Contrast(rgb_img).enhance(contraste)
    rgb_img = ImageEnhance.Color(rgb_img).enhance(saturacion)
    rgb_img = ImageEnhance.Sharpness(rgb_img).enhance(nitidez)

    r2, g2, b2 = rgb_img.split()
    return Image.merge("RGBA", (r2, g2, b2, a))


def _sombra_contorno(alpha_arr: np.ndarray, intensidad: float) -> Image.Image:
    """
    Sombra que reproduce la SILUETA exacta de la prenda (no una elipse
    genérica), desplazada un poco y difuminada, para que la prenda se vea
    "elevada" con profundidad real — sin esto, el recorte se ve como una
    pegatina plana pegada al fondo, por más sombra de suelo que tenga.
    """
    h, w = alpha_arr.shape
    capa = Image.fromarray(alpha_arr.astype("uint8"), mode="L")

    desplazamiento = max(2, int(min(w, h) * 0.012))
    radio_blur = max(3, int(min(w, h) * 0.024))

    sombra = Image.new("L", (w, h), 0)
    sombra.paste(capa, (desplazamiento, int(desplazamiento * 1.4)))
    sombra = sombra.filter(ImageFilter.GaussianBlur(radius=radio_blur))

    sombra_arr = np.array(sombra).astype(np.float32) / 255.0 * intensidad
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 3] = np.clip(sombra_arr * 255, 0, 255).astype("uint8")
    return Image.fromarray(rgba, mode="RGBA")


def _dibujar_sombra(fondo: Image.Image, alpha_arr: np.ndarray, intensidad: float) -> Image.Image:
    """
    Sombra en dos capas, como en un estudio real:
    - "Contacto": sombra pequeña, más oscura y definida, pegada a la base.
    - "Ambiental": sombra grande, muy difuminada, da sensación de profundidad.
    """
    h, w = alpha_arr.shape
    mask = alpha_arr > 30
    ys, xs = np.where(mask)
    if len(xs) == 0 or intensidad <= 0:
        return fondo

    x0, x1 = xs.min(), xs.max()
    y1 = ys.max()
    cx = (x0 + x1) / 2
    ancho_base = x1 - x0

    capa = Image.new("RGBA", fondo.size, (0, 0, 0, 0))

    # Sombra de contacto: estrecha, más oscura, muy pegada al borde inferior
    draw = ImageDraw.Draw(capa)
    ancho_contacto = ancho_base * 0.55
    alto_contacto = ancho_contacto * 0.07
    draw.ellipse(
        [cx - ancho_contacto / 2, y1 - alto_contacto / 2, cx + ancho_contacto / 2, y1 + alto_contacto / 2],
        fill=(0, 0, 0, int(255 * min(intensidad * 1.3, 1.0))),
    )
    capa = capa.filter(ImageFilter.GaussianBlur(radius=max(alto_contacto * 1.1, 2)))

    # Sombra ambiental: ancha, suave, da profundidad general
    capa_amb = Image.new("RGBA", fondo.size, (0, 0, 0, 0))
    draw_amb = ImageDraw.Draw(capa_amb)
    ancho_amb = ancho_base * 0.85
    alto_amb = ancho_amb * 0.20
    draw_amb.ellipse(
        [cx - ancho_amb / 2, y1 - alto_amb / 2, cx + ancho_amb / 2, y1 + alto_amb / 2],
        fill=(0, 0, 0, int(255 * intensidad * 0.6)),
    )
    capa_amb = capa_amb.filter(ImageFilter.GaussianBlur(radius=alto_amb * 1.1))

    fondo_rgba = fondo.convert("RGBA")
    fondo_rgba = Image.alpha_composite(fondo_rgba, capa_amb)
    fondo_rgba = Image.alpha_composite(fondo_rgba, capa)
    return fondo_rgba.convert("RGB")


def crear_fondo_estudio(color_base: Tuple[int, int, int], size: Tuple[int, int]) -> Image.Image:
    """
    Fondo con degradado radial sutil, como el papel continuo de un estudio
    fotográfico real: más claro en el centro, ligeramente más oscuro hacia
    las esquinas. Un color plano se ve "de pantalla"; esto da profundidad.
    """
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h * 0.42  # centro del degradado un poco por encima del centro real
    dist = np.sqrt(((xx - cx) / (w * 0.75)) ** 2 + ((yy - cy) / (h * 0.75)) ** 2)
    dist = np.clip(dist, 0, 1)
    falloff = 1 - (dist ** 2) * 0.16  # hasta ~16% más oscuro en las esquinas

    base = np.array(color_base, dtype=np.float32)
    arr = base[None, None, :] * falloff[:, :, None]
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), mode="RGB")


def _despill_verde(rgba: Image.Image) -> Image.Image:
    """
    Quita el 'spill' verde: en los píxeles de borde semitransparentes que
    quedan tras la segmentación, el color original todavía lleva mezclado
    algo del verde de fondo. Sin esto, al componer sobre un fondo nuevo
    se nota un halo verdoso alrededor de la prenda.
    """
    arr = np.array(rgba).astype(np.float32)
    r, g, b, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    exceso_verde = g - np.maximum(r, b)
    exceso_verde = np.clip(exceso_verde, 0, None)
    g_corregido = g - exceso_verde
    arr[..., 1] = np.clip(g_corregido, 0, 255)
    return Image.fromarray(arr.astype("uint8"), mode="RGBA")


def _erosionar_alpha(rgba: Image.Image, px: int = 2) -> Image.Image:
    """Encoge ligeramente el borde de la máscara para descartar el anillo
    de píxeles semitransparentes más contaminados por el fondo original."""
    if px <= 0:
        return rgba
    r, g, b, a = rgba.split()
    k = px * 2 + 1
    a = a.filter(ImageFilter.MinFilter(k))
    return Image.merge("RGBA", (r, g, b, a))


def procesar_imagen_ia(
    im: Image.Image, session, fondo: Image.Image, p: ParametrosIA
) -> Image.Image:
    im = im.convert("RGB")
    im = ImageOps.exif_transpose(im)
    if p.rotar_a_vertical and im.width > im.height:
        im = im.rotate(-90, expand=True)
    if p.rotacion_manual:
        im = im.rotate(-p.rotacion_manual, expand=True)

    recorte_ia = remove(im, session=session)  # RGBA con alpha real por IA
    recorte_ia = recorte_ia.convert("RGBA")  # por si alguna versión de rembg devolviera otro modo
    recorte_ia = _despill_verde(recorte_ia)
    recorte_ia = _erosionar_alpha(recorte_ia, px=2)
    if p.mejorar_color:
        recorte_ia = _mejorar_color(
            recorte_ia, p.intensidad_contraste, p.intensidad_saturacion, p.intensidad_nitidez
        )
    alpha_arr = np.array(recorte_ia)[:, :, 3]

    # Recorte AJUSTADO (sin margen todavía) a la silueta real de la prenda
    mask = alpha_arr > 30
    ys, xs = np.where(mask)
    if len(xs) == 0:
        raise ValueError("No se detectó ninguna prenda en la foto")
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max(), ys.max()

    if p.recortar_ajustado:
        recorte_final = recorte_ia.crop((x0, y0, x1, y1))
    else:
        recorte_final = recorte_ia
    fg_w, fg_h = recorte_final.size

    if p.tamano_fijo:
        # Lienzo IDÉNTICO para toda la tanda: la prenda se escala (sin
        # deformar ni recortar) para caber dentro, dejando el margen
        # indicado, y se centra. Así las 30 fotos salen todas igual de
        # tamaño y encuadre, como en una tienda profesional.
        out_w, out_h = p.tamano_fijo
        max_fg_w = out_w * (1 - 2 * p.margen_recorte_pct)
        max_fg_h = out_h * (1 - 2 * p.margen_recorte_pct)
        escala = min(max_fg_w / fg_w, max_fg_h / fg_h)
        nuevo_w = max(1, int(fg_w * escala))
        nuevo_h = max(1, int(fg_h * escala))
        recorte_final = recorte_final.resize((nuevo_w, nuevo_h), Image.LANCZOS)
        fg_w, fg_h = nuevo_w, nuevo_h
        pad_x = (out_w - fg_w) // 2
        pad_y = (out_h - fg_h) // 2
    else:
        # El margen crea LIENZO NUEVO más grande, no depende de que la
        # foto original tuviera espacio de sobra alrededor de la prenda.
        pad_x = int(fg_w * p.margen_recorte_pct)
        pad_y = int(fg_h * p.margen_recorte_pct)
        out_w, out_h = fg_w + pad_x * 2, fg_h + pad_y * 2

    ratio = max(out_w / fondo.width, out_h / fondo.height)
    fondo_resized = fondo.resize((int(fondo.width * ratio) + 1, int(fondo.height * ratio) + 1))
    off_x = (fondo_resized.width - out_w) // 2
    off_y = (fondo_resized.height - out_h) // 2
    fondo_final = fondo_resized.crop((off_x, off_y, off_x + out_w, off_y + out_h))

    # Máscara de alpha reubicada en las coordenadas del lienzo final (con margen)
    alpha_lienzo = np.zeros((out_h, out_w), dtype=np.float32)
    alpha_recorte = np.array(recorte_final)[:, :, 3]
    alpha_lienzo[pad_y:pad_y + fg_h, pad_x:pad_x + fg_w] = alpha_recorte

    if p.añadir_sombra:
        fondo_final = _dibujar_sombra(fondo_final, alpha_lienzo, p.intensidad_sombra)

    resultado = fondo_final.convert("RGBA")

    if p.sombra_contorno:
        sombra_silueta = _sombra_contorno(alpha_lienzo, p.intensidad_contorno)
        resultado = Image.alpha_composite(resultado, sombra_silueta)

    resultado.alpha_composite(recorte_final, (pad_x, pad_y))
    return resultado.convert("RGB")
