"""Buzón de facturas: clasificación de adjuntos, procesado de correos, idempotencia y conexión IMAP simulada."""
from email.message import EmailMessage

from core import buzon, db
from core.pdf_simple import pdf_de_texto


def _factura(num="F-2026/117", base="1.000,00", nif="B12345674"):
    return pdf_de_texto(f"FACTURA Nº {num}", [
        f"Emisor: Subcontratas Prueba SL · NIF {nif}", "Cliente: LLORCA GROUP HISPANIA, S.L. · NIF B54727722",
        "Fecha de factura: 15/09/2026   Vencimiento: 15/11/2026", "Obra 901 Residencial Prueba",
        "Concepto: trabajos de albañilería septiembre", f"Base imponible {base} €", "IVA inversión del sujeto pasivo",
        f"Total factura {base} €", "Forma de pago: transferencia IBAN ES91 2100 0418 4502 0005 1332"] + ["Detalle de trabajos realizados en obra."] * 6)


def _presupuesto():
    return pdf_de_texto("PRESUPUESTO Nº 55", ["Oferta de trabajos de pintura para la obra", "Validez de la oferta 30 días",
                                                "Importe total 12.500,00 €", "Condiciones: 50 % a la aceptación"] + ["Texto de relleno de la oferta."] * 8)


def _correo(adjuntos, asunto="Factura septiembre", remitente="admin@subcontratasprueba.es", mid="<m1@prueba>"):
    m = EmailMessage()
    m["From"] = f"Administración <{remitente}>"
    m["To"] = "facturas@llorca.es"
    m["Subject"] = asunto
    m["Message-ID"] = mid
    m["Date"] = "Tue, 15 Sep 2026 10:00:00 +0200"
    m.set_content("Adjuntamos documentación.")
    for nombre, data in adjuntos:
        m.add_attachment(data, maintype="application", subtype="pdf", filename=nombre)
    return bytes(m)


def test_clasifica_factura_y_otros_documentos():
    assert buzon.clasificar_texto("FACTURA Nº 12 Base imponible 100,00 IVA 21% Total factura 121,00 NIF B12345674")[0] == "factura"
    assert buzon.clasificar_texto("PRESUPUESTO nº 5. Oferta válida 30 días. Total 1.000 €")[0] == "no_factura"
    assert buzon.clasificar_texto("FACTURA PROFORMA 33 base imponible 10 IVA total")[0] != "factura"
    assert buzon.clasificar_texto("Nómina de agosto. Recibo de salarios. Líquido a percibir")[0] == "no_factura"
    # una factura que cita el albarán sigue siendo factura
    assert buzon.clasificar_texto("FACTURA 7 albarán 123 base imponible 50 IVA 21 total factura NIF B12345674")[0] == "factura"


def test_procesa_correo_importa_solo_facturas(con, obra):
    db.set_setting(con, "buzon_leer_auto", "0")
    raw = _correo([("fra_117.pdf", _factura()), ("presupuesto.pdf", _presupuesto())])
    r = buzon.procesar_mensaje(con, raw, "1", "INBOX")
    assert r["estado"] == "procesado" and r["facturas"] == 1
    adj = db.rows(con, "SELECT clasificacion, documento_id, ruta FROM buzon_adjuntos ORDER BY id")
    assert [a["clasificacion"] for a in adj] == ["factura", "no_factura"]
    doc = db.one(con, "SELECT * FROM documentos WHERE id=?", (adj[0]["documento_id"],))
    assert doc["buzon_mensaje_id"] == r["mensaje_id"] and "Recibida por correo" in doc["notas"]
    # el mismo correo no se procesa dos veces
    assert buzon.procesar_mensaje(con, raw, "1", "INBOX")["estado"] == "ya_procesado"
    # el mismo PDF en otro correo: ya estaba
    r2 = buzon.procesar_mensaje(con, _correo([("reenvio.pdf", _factura())], mid="<m2@prueba>"), "2", "INBOX")
    assert r2["facturas"] == 0
    assert db.one(con, "SELECT clasificacion FROM buzon_adjuntos WHERE mensaje_id=?", (r2["mensaje_id"],))["clasificacion"] == "duplicado"
    # el presupuesto apartado se puede importar a mano
    did = buzon.importar_apartado(con, db.one(con, "SELECT id FROM buzon_adjuntos WHERE clasificacion='no_factura'")["id"], "ana", leer=False)
    assert db.one(con, "SELECT buzon_mensaje_id FROM documentos WHERE id=?", (did,))["buzon_mensaje_id"] == r["mensaje_id"]


def test_encola_lectura_y_remitente_ignorado(con, obra):
    db.set_setting(con, "buzon_ignorar", "boletin@publicidad.com")
    r = buzon.procesar_mensaje(con, _correo([("f.pdf", _factura("A-1"))], remitente="boletin@publicidad.com", mid="<x@p>"), "3", "INBOX")
    assert r["estado"] == "ignorado"
    r = buzon.procesar_mensaje(con, _correo([("f.pdf", _factura("A-2"))], mid="<y@p>"), "4", "INBOX")
    assert r["trabajo"] and db.one(con, "SELECT total FROM trabajos WHERE id=?", (r["trabajo"],))["total"] == 1


def test_correo_sin_adjuntos(con):
    r = buzon.procesar_mensaje(con, _correo([], mid="<vacio@p>"), "5", "INBOX")
    assert r["estado"] == "sin_adjuntos"


def test_aprende_remitente_y_completa_nif(con, obra, proveedor):
    db.set_setting(con, "buzon_leer_auto", "0")
    r = buzon.procesar_mensaje(con, _correo([("f.pdf", _factura("B-1"))], mid="<a@p>"), "6", "INBOX")
    d1 = r["documentos"][0]
    con.execute("UPDATE documentos SET estado='aprobada', proveedor_id=?, aprobado_en='2026-09-20' WHERE id=?", (proveedor, d1))
    con.commit()
    assert buzon.aprender_remitentes(con) == 1
    # otra factura del mismo remitente sin NIF legible (CIF en el logotipo)
    r = buzon.procesar_mensaje(con, _correo([("g.pdf", _factura("B-2", nif="(en logotipo)"))], mid="<b@p>"), "7", "INBOX")
    d2 = r["documentos"][0]
    assert buzon.completar_por_remitente(con, d2)
    d = db.one(con, "SELECT proveedor_id, emisor_nif, observaciones_ia FROM documentos WHERE id=?", (d2,))
    assert d["proveedor_id"] == proveedor and d["emisor_nif"] == "B12345674" and "remitente" in d["observaciones_ia"]


class _IMAPFalso:
    """Servidor IMAP mínimo en memoria para probar la vuelta completa sin red."""
    buzones = {}

    def __init__(self, host, port, timeout=None):
        self.capabilities = ("IMAP4REV1", "MOVE")
        self.sel = None

    def login(self, u, p):
        assert p == "secreto"

    def select(self, carpeta, readonly=False):
        self.sel = carpeta.strip('"')
        return ("OK", [str(len(self.buzones.get(self.sel, {}))).encode()]) if self.sel in self.buzones else ("NO", [b""])

    def uid(self, cmd, *args):
        b = self.buzones[self.sel]
        if cmd == "SEARCH":
            return "OK", [" ".join(k for k, v in b.items() if "\\Seen" not in v["flags"]).encode()]
        if cmd == "FETCH":
            return "OK", [(b"1 (BODY[] {n}", b[args[0]]["raw"]), b")"]
        if cmd == "STORE":
            b[args[0]]["flags"].add("\\Seen")
            return "OK", []
        if cmd == "MOVE":
            destino = args[1].strip('"')
            self.buzones.setdefault(destino, {})[args[0]] = b.pop(args[0])
            return "OK", []
        raise AssertionError(cmd)

    def search(self, charset, criterio):
        return "OK", [" ".join(k for k, v in self.buzones[self.sel].items() if "\\Seen" not in v["flags"]).encode()]

    def logout(self):
        pass


def test_vuelta_imap_completa(con, obra, monkeypatch):
    from core import credenciales
    _IMAPFalso.buzones = {"INBOX": {"10": {"raw": _correo([("f.pdf", _factura("C-1"))], mid="<i1@p>"), "flags": set()},
                                    "11": {"raw": _correo([("p.pdf", _presupuesto())], mid="<i2@p>"), "flags": set()}},
                          "Procesadas": {}}
    monkeypatch.setattr(buzon.imaplib, "IMAP4_SSL", _IMAPFalso)
    for k, v in {"host": "imap.prueba", "usuario": "facturas@llorca.es", "carpeta_procesados": "Procesadas", "leer_auto": "0"}.items():
        db.set_setting(con, "buzon_" + k, v)
    credenciales.guardar(con, "buzon_clave", "secreto")
    assert credenciales.leer(con, "buzon_clave") == "secreto"
    assert db.get_setting(con, "secreto_buzon_clave") != "secreto"          # se guarda cifrada
    res = buzon.revisar(con)
    assert "2 correo(s)" in res and "1 factura(s)" in res
    assert set(_IMAPFalso.buzones["Procesadas"]) == {"10", "11"} and not _IMAPFalso.buzones["INBOX"]
    assert buzon.probar_conexion(con)[0]


def test_tarea_registrada_en_planificador(con):
    from core import planificador
    planificador.registrar_todas()
    assert {"buzon", "respaldo", "correo", "sis", "actas"} <= {t["nombre"] for t in planificador.estado(con)}
    assert "buzon" not in planificador.pendientes(con)                     # desactivado por defecto
    db.set_setting(con, "buzon_activo", "1")
    assert "buzon" in planificador.pendientes(con)


def test_zip_desmesurado_no_se_abre(con, obra):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("grande.pdf", b"\0" * (buzon.MAX_ZIP_DESCOMPRIMIDO_MB * 1024 * 1024 + 10))
    m = EmailMessage()
    m["From"] = "x@y.es"
    m["Message-ID"] = "<zip@p>"
    m.set_content("zip")
    m.add_attachment(buf.getvalue(), maintype="application", subtype="zip", filename="facturas.zip")
    r = buzon.procesar_mensaje(con, bytes(m), "9", "INBOX")
    a = db.one(con, "SELECT clasificacion, motivos FROM buzon_adjuntos WHERE mensaje_id=?", (r["mensaje_id"],))
    assert a["clasificacion"] == "error" and "bomba" in a["motivos"]
