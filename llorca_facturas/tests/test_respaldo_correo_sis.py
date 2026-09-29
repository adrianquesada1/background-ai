"""Copia de seguridad automática, correo saliente y envío de asientos a SIS."""
import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from core import db, respaldo, correo, sis, planificador, ingesta, maestros


# ============================================================================ copias
def test_copia_verificada_espejo_y_restauracion(con, obra, tmp_path, monkeypatch):
    datos = tmp_path / "datos"
    (datos / "pdfs").mkdir(parents=True)
    (datos / "pdfs" / "a.pdf").write_bytes(b"%PDF-1.4 prueba")
    (datos / ".clave_secretos").write_bytes(b"no se copia")
    monkeypatch.setattr(respaldo, "DATA_DIR", datos)
    destino = tmp_path / "nas"
    db.set_setting(con, "respaldo_destinos", str(destino))
    res = respaldo.copiar(con)
    assert "verificada" in res
    copias = respaldo.listar_copias(str(destino))
    assert len(copias) == 1 and copias[0]["sha256"]
    assert (destino / "archivos" / "pdfs" / "a.pdf").exists()
    assert not (destino / "archivos" / ".clave_secretos").exists()
    ok, det = respaldo.simulacro(copias[0]["archivo"])
    assert ok, det
    # la segunda copia no vuelve a copiar archivos que no han cambiado
    assert db.one(con, "SELECT archivos_copiados FROM respaldos ORDER BY id DESC")["archivos_copiados"] == 1
    respaldo.copiar(con)
    assert db.one(con, "SELECT archivos_copiados FROM respaldos ORDER BY id DESC")["archivos_copiados"] == 0
    # restauración sobre otra base: conserva la anterior al lado
    otra = tmp_path / "restaurada.db"
    otra.write_bytes(b"")
    respaldo.restaurar(copias[0]["archivo"], otra)
    import sqlite3
    assert sqlite3.connect(otra).execute("SELECT COUNT(*) FROM obras").fetchone()[0] >= 1


def test_copia_danada_no_se_restaura(con, tmp_path, monkeypatch):
    monkeypatch.setattr(respaldo, "DATA_DIR", tmp_path / "d")
    (tmp_path / "d").mkdir()
    db.set_setting(con, "respaldo_destinos", str(tmp_path / "c"))
    respaldo.copiar(con)
    arch = respaldo.listar_copias(str(tmp_path / "c"))[0]["archivo"]
    b = bytearray(Path(arch).read_bytes())
    b[len(b) // 2] ^= 0xFF
    Path(arch).write_bytes(bytes(b))
    assert not respaldo.simulacro(arch)[0]
    with pytest.raises(RuntimeError):
        respaldo.restaurar(arch, tmp_path / "x.db")


def test_rotacion_abuelo_padre_hijo():
    base = datetime(2026, 1, 1, 21, 30)
    fechas = [base + timedelta(days=i) for i in range(200)] + [base + timedelta(days=199, hours=2)]
    keep = respaldo.a_conservar(fechas, diarias=7, semanales=4, mensuales=6)
    assert max(fechas) in keep
    assert len([f for f in keep if f >= base + timedelta(days=193)]) >= 7
    assert len(keep) <= 7 + 4 + 6 + 1


def test_tarea_copia_diaria(con):
    db.set_setting(con, "respaldo_hora", "21:30")
    hoy = datetime(2026, 9, 29, 22, 0)
    con.execute("INSERT INTO respaldos (inicio, fin, ok) VALUES ('2026-09-28T21:30:00','2026-09-28T21:31:00',1)")
    assert respaldo.toca(con, datetime(2026, 9, 28, 21, 31), hoy)
    assert not respaldo.toca(con, datetime(2026, 9, 29, 21, 31), hoy)
    assert not respaldo.toca(con, datetime(2026, 9, 29, 10, 0), datetime(2026, 9, 29, 12, 0))


def test_avisos_copia(con, tmp_path, monkeypatch):
    monkeypatch.setattr(respaldo, "DATA_DIR", tmp_path)
    db.set_setting(con, "respaldo_destinos", str(tmp_path / "copias"))
    av = respaldo.avisos(con)
    assert any("ninguna copia" in a for a in av) and any("MISMO disco" in a for a in av)


# ============================================================================ correo
def test_bandeja_aprobacion_y_envio_prueba(con, tmp_path, monkeypatch):
    monkeypatch.setattr(correo, "carpeta_prueba", lambda: tmp_path)
    db.set_setting(con, "smtp_modo", "prueba")
    adj = tmp_path / "doc.pdf"
    adj.write_bytes(b"%PDF-1.4")
    cid = correo.preparar(con, "reclamacion_retencion", "cliente@ejemplo.es", "Reclamación", "Texto", "ana", adjuntos=[adj],
                          origen="factura_emitida", origen_id=5)
    assert correo.preparar(con, "reclamacion_retencion", "cliente@ejemplo.es", "Reclamación", "Texto", "ana", origen="factura_emitida",
                           origen_id=5) == cid                                  # no se duplica
    assert correo.enviar_pendientes(con) == "Nada que enviar."                 # espera aprobación
    db.set_setting(con, "smtp_cuatro_ojos", "1")
    with pytest.raises(ValueError):
        correo.aprobar(con, cid, "ana", "gestor")
    correo.aprobar(con, cid, "luis", "direccion")
    assert "1 enviado" in correo.enviar_pendientes(con)
    eml = (tmp_path / f"correo_{cid:06d}.eml").read_bytes()
    assert b"cliente@ejemplo.es" in eml and b"doc.pdf" in eml
    assert db.one(con, "SELECT estado FROM correo_salida WHERE id=?", (cid,))["estado"] == "enviado"


def test_tipos_automaticos_y_direcciones(con):
    with pytest.raises(ValueError):
        correo.preparar(con, "otro", "no-es-un-correo", "a", "b", "ana")
    cid = correo.preparar(con, "aviso_pago", "prov@ejemplo.es", "Aviso", "Pagado", "ana")
    assert db.one(con, "SELECT estado FROM correo_salida WHERE id=?", (cid,))["estado"] == "aprobado"


def test_smtp_real_con_reintento(con, monkeypatch):
    enviados = []

    class SMTPFalso:
        fallar = True

        def __init__(self, host, port, timeout=None):
            pass

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            assert p == "clave-smtp"

        def send_message(self, m, to_addrs=None):
            if SMTPFalso.fallar:
                SMTPFalso.fallar = False
                raise OSError("red caída")
            enviados.append((m["Subject"], to_addrs))
            return {}

        def quit(self):
            pass
    from core import credenciales
    monkeypatch.setattr(correo.smtplib, "SMTP", SMTPFalso)
    for k, v in {"modo": "real", "host": "smtp.prueba", "usuario": "facturas@llorca.es", "copia_oculta": "archivo@llorca.es"}.items():
        db.set_setting(con, "smtp_" + k, v)
    credenciales.guardar(con, "smtp_clave", "clave-smtp")
    cid = correo.preparar(con, "aviso_pago", "prov@ejemplo.es", "Aviso de pago", "Pagado", "ana")
    assert "1 con error" in correo.enviar_pendientes(con)
    con.execute("UPDATE correo_salida SET proximo_intento=NULL WHERE id=?", (cid,))
    assert "1 enviado" in correo.enviar_pendientes(con)
    assert enviados == [("Aviso de pago", ["prov@ejemplo.es", "archivo@llorca.es"])]


# ============================================================================ SIS
def _factura_aprobada(con, obra, proveedor, num="F-1", base=100000):
    did, _ = ingesta.registrar_pdf(con, f"{num}.pdf", b"%PDF-1.4 " + num.encode() * 50, "t")
    con.execute("""UPDATE documentos SET estado='aprobada', tipo_documento='factura', proveedor_id=?, obra_id=?, numero=?, fecha='2026-09-15',
                   emisor_nif='B12345674', base_imponible_cents=?, total_factura_cents=?, total_a_pagar_cents=?, inversion_sujeto_pasivo=1,
                   aprobado_en='2026-09-20T10:00:00', cuenta_contable='607' WHERE id=?""", (proveedor, obra, num, base, base, base, did))
    con.commit()
    return did


class _SIS(BaseHTTPRequestHandler):
    recibidos = []

    def do_POST(self):
        cuerpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ref = self.headers.get("Idempotency-Key")
        repetido = any(r["referencia_externa"] == ref for r in self.recibidos)
        self.recibidos.append(cuerpo)
        self.send_response(409 if repetido else 201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"id": len(self.recibidos)}).encode())

    def log_message(self, *a):
        pass


def test_envio_sis_idempotente_y_desfasado(con, obra, proveedor):
    srv = HTTPServer(("127.0.0.1", 0), _SIS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for k, v in {"modo": "real", "url": f"http://127.0.0.1:{srv.server_port}", "desde": "2026-01-01", "empresa": "LLORCA"}.items():
            db.set_setting(con, "sis_" + k, v)
        did = _factura_aprobada(con, obra, proveedor)
        res = sis.enviar(con)
        assert "1 contabilizado" in res, res
        env = db.one(con, "SELECT * FROM sis_envios WHERE documento_id=?", (did,))
        assert env["estado"] == "enviado" and env["id_sis"] == "1"
        cuerpo = _SIS.recibidos[0]
        assert cuerpo["empresa"] == "LLORCA" and len(cuerpo["apuntes"]) >= 2
        debe = sum(float(a["debe"]) for a in cuerpo["apuntes"])
        haber = sum(float(a["haber"]) for a in cuerpo["apuntes"])
        assert abs(debe - haber) < 0.005
        assert "0 contabilizado" in sis.enviar(con)                       # no se reenvía
        # la factura cambia tras contabilizarla
        con.execute("UPDATE documentos SET base_imponible_cents=90000, total_a_pagar_cents=90000, total_factura_cents=90000 WHERE id=?", (did,))
        con.commit()
        assert "1 desfasada" in sis.enviar(con)
        sis.marcar(con, env["id"], "enviado", "ana", "ajustado a mano en SIS")
        assert db.one(con, "SELECT estado FROM sis_envios WHERE id=?", (env["id"],))["estado"] == "enviado"
    finally:
        srv.shutdown()


def test_sis_modo_prueba_no_envia(con, obra, proveedor, tmp_path, monkeypatch):
    monkeypatch.setattr(sis, "carpeta_prueba", lambda: tmp_path)
    db.set_setting(con, "sis_modo", "prueba")
    db.set_setting(con, "sis_desde", "2026-01-01")
    did = _factura_aprobada(con, obra, proveedor, "F-9")
    sis.enviar(con)
    assert list(tmp_path.glob("LLORCA-*.json"))
    assert db.one(con, "SELECT estado FROM sis_envios WHERE documento_id=?", (did,))["estado"] == "pendiente"


def test_planificador_turno_y_error(con):
    llamadas = []
    planificador.registrar(planificador.Tarea("prueba_falla", "Prueba", lambda c, u, a: True, lambda c: 1 / 0))
    planificador.registrar(planificador.Tarea("prueba_ok", "Prueba", lambda c, u, a: True, lambda c: llamadas.append(1) or "hecho"))
    r = planificador.ejecutar_ahora(con, "prueba_falla")
    assert not r["ok"] and "ZeroDivisionError" in r["error"]
    assert planificador.ejecutar_ahora(con, "prueba_ok")["resultado"] == "hecho"
    # turno ocupado por otro proceso: no se ejecuta dos veces
    con.execute("UPDATE tareas_programadas SET turno='otro', turno_hasta=? WHERE nombre='prueba_ok'",
                ((datetime.now() + timedelta(minutes=5)).isoformat(),))
    con.commit()
    assert not planificador.ejecutar_ahora(con, "prueba_ok")["ok"]
    assert len(llamadas) == 1
    assert planificador.historial(con, "prueba_falla")[0]["ok"] == 0
