"""
Generador mínimo de PDF (sin dependencias): texto en Helvetica, negrita, líneas y tablas sencillas, varias páginas.

Sirve para los documentos que la aplicación emite (autorización de facturación al subcontratista, actas…). Codifica en
WinAnsi (cp1252), así que admite tildes, ñ, € y ª/º. Los PDF resultantes tienen capa de texto: se pueden buscar y la
propia aplicación los vuelve a leer sin OCR.
"""
from __future__ import annotations

A4 = (595.28, 841.89)
_ANCHO_MEDIO = {"Helvetica": 0.52, "Helvetica-Bold": 0.56}


def _esc(t: str) -> bytes:
    b = str(t).encode("cp1252", errors="replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"").replace(b"\n", b" ")


def ancho_texto(t: str, tam: float, fuente: str = "Helvetica") -> float:
    return len(str(t)) * tam * _ANCHO_MEDIO.get(fuente, 0.52)


class Documento:
    def __init__(self, margen: float = 50, tam: float = 9.5, titulo: str = ""):
        self.w, self.h = A4
        self.m = margen
        self.tam = tam
        self.titulo = titulo
        self.paginas: list[list[bytes]] = []
        self.y = 0.0
        self.nueva_pagina()

    # ---------------------------------------------------------------- primitivas
    def nueva_pagina(self):
        self.paginas.append([])
        self.y = self.h - self.m

    def _op(self, s: bytes):
        self.paginas[-1].append(s)

    def texto(self, x: float, y: float, t: str, tam: float | None = None, negrita: bool = False, derecha: bool = False):
        tam = tam or self.tam
        f = "F2" if negrita else "F1"
        if derecha:
            x = x - ancho_texto(t, tam, "Helvetica-Bold" if negrita else "Helvetica")
        self._op(b"BT /" + f.encode() + b" %.1f Tf %.2f %.2f Td (" % (tam, x, y) + _esc(t) + b") Tj ET")

    def linea(self, x1, y1, x2, y2, grosor: float = 0.5):
        self._op(b"%.2f w %.2f %.2f m %.2f %.2f l S" % (grosor, x1, y1, x2, y2))

    def espacio(self, alto: float):
        if self.y - alto < self.m:
            self.nueva_pagina()
        self.y -= alto

    # ---------------------------------------------------------------- bloques
    def parrafo(self, t: str, tam: float | None = None, negrita: bool = False, sangria: float = 0, interlineado: float = 1.35):
        tam = tam or self.tam
        ancho = self.w - 2 * self.m - sangria
        for bloque in str(t).split("\n"):
            palabras, linea = bloque.split(" "), ""
            lineas = []
            for p in palabras:
                prueba = (linea + " " + p).strip()
                if ancho_texto(prueba, tam) > ancho and linea:
                    lineas.append(linea)
                    linea = p
                else:
                    linea = prueba
            lineas.append(linea)
            for li in lineas:
                self.espacio(tam * interlineado)
                self.texto(self.m + sangria, self.y, li, tam, negrita)

    def titulo_seccion(self, t: str, tam: float = 12):
        self.espacio(tam * 0.8)
        self.parrafo(t, tam, negrita=True)
        self.espacio(2)
        self.linea(self.m, self.y, self.w - self.m, self.y, 0.8)
        self.espacio(4)

    def tabla(self, cabecera: list[str], filas: list[list], anchos: list[float], derecha: set[int] | None = None,
              tam: float | None = None, negrita_ultima: bool = False):
        """Tabla simple. `anchos` en proporción del ancho útil. Corta textos largos para que quepan en su columna."""
        tam = tam or self.tam - 0.5
        derecha = derecha or set()
        util = self.w - 2 * self.m
        xs, acc = [], self.m
        for a in anchos:
            xs.append(acc)
            acc += util * a / sum(anchos)
        xs.append(self.m + util)

        def fila(valores, negrita=False):
            self.espacio(tam * 1.5)
            for i, v in enumerate(valores):
                v = "" if v is None else str(v)
                maxc = max(3, int((xs[i + 1] - xs[i] - 4) / (tam * 0.52)))
                if len(v) > maxc:
                    v = v[: maxc - 1] + "…"
                if i in derecha:
                    self.texto(xs[i + 1] - 3, self.y, v, tam, negrita, derecha=True)
                else:
                    self.texto(xs[i] + 2, self.y, v, tam, negrita)

        fila(cabecera, negrita=True)
        self.espacio(3)
        self.linea(self.m, self.y + 1, self.m + util, self.y + 1)
        for k, f in enumerate(filas):
            fila(f, negrita=negrita_ultima and k == len(filas) - 1)

    # ---------------------------------------------------------------- salida
    def bytes(self) -> bytes:
        objs: list[bytes] = []

        def add(o: bytes) -> int:
            objs.append(o)
            return len(objs)

        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        pages_id = len(objs) + 1 + 2 * len(self.paginas)   # se reserva al final
        kids = []
        total = len(self.paginas)
        for n, ops in enumerate(self.paginas, 1):
            pie = b"BT /F1 7.5 Tf %.2f %.2f Td (" % (self.m, 25) + _esc(f"{self.titulo}  ·  página {n} de {total}") + b") Tj ET"
            contenido = b"\n".join(ops + [pie])
            c = add(b"<< /Length %d >>\nstream\n" % len(contenido) + contenido + b"\nendstream")
            p = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Contents %d 0 R "
                    b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>" % (pages_id, self.w, self.h, c, f1, f2))
            kids.append(p)
        pid = add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids))
        assert pid == pages_id
        cat = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pid)
        info = add(b"<< /Title (" + _esc(self.titulo) + b") /Producer (Llorca Group - Control economico de obra) >>")
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offs = []
        for i, o in enumerate(objs, 1):
            offs.append(len(out))
            out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for o in offs:
            out += b"%010d 00000 n \n" % o
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, info, xref)
        return bytes(out)


def pdf_de_texto(titulo: str, lineas: list[str]) -> bytes:
    """PDF de una o varias páginas con un título y párrafos (útil para pruebas y documentos sencillos)."""
    d = Documento(titulo=titulo)
    d.parrafo(titulo, 14, negrita=True)
    d.espacio(6)
    for li in lineas:
        d.parrafo(li)
    return d.bytes()
