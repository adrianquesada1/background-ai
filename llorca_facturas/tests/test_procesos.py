"""Procesos nuevos: certificación a proveedor, preparación de la certificación al cliente, planificación, planos,
seguridad y salud, conciliación bancaria y actas desde audio."""
import io
import json
from datetime import date
from decimal import Decimal

import pytest

from core import db, cert_proveedor as CP, cert_cliente as CC, planificacion as PL, planos as PN, prevencion as PV
from core import conciliacion as BC, actas_audio as AA, obra_control, casacion, ingesta, pagos, tesoreria


# ============================================================================ utilidades
def _contrato(con, obra, proveedor, importe=1_000_000):
    return obra_control.guardar_oferta(con, {"obra_id": obra, "proveedor_id": proveedor, "proveedor_nombre": "Subcontratas Prueba SL",
                                             "alcance": "Albañilería", "importe_cents": importe, "estado": "adjudicada"}, "t")


def _cert_cliente(con, obra):
    """Certificación al cliente mínima (nº 1) con dos partidas."""
    cur = con.execute("INSERT INTO certificaciones (obra_id, numero, fecha, total_origen_m, total_anterior_m, total_actual_m) VALUES (?,?,?,?,?,?)",
                      (obra, 1, "2026-08-31", 0, 0, 0))
    cid = cur.lastrowid
    con.execute("INSERT INTO cert_capitulos (cert_id, codigo, nombre, nivel, orden, tipo, origen_m, anterior_m, actual_m) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, "05", "ALBAÑILERÍA", 1, 1, "contrato", 0, 0, 0))
    for orden, (cod, ud, precio, pres) in enumerate([("05.01", "m2", "30.000", "1000.000"), ("05.02", "m2", "12.000", "500.000")]):
        con.execute("""INSERT INTO cert_lineas (cert_id, orden, capitulo, codigo, unidad, titulo, pct_origen, cant_origen, precio, cant_presupuesto,
                       origen_m, anterior_m, actual_m, presupuesto_m, cant_anterior, cant_actual, tipo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, orden, "05", cod, ud, f"Partida {cod}", "0", "0", precio, pres, 0, 0, 0, 0, "0", "0", "contrato"))
    con.commit()
    return cid


# ============================================================================ certificación a proveedor
def test_ciclo_certificacion_proveedor(con, obra, proveedor, tmp_path, monkeypatch):
    monkeypatch.setattr(CP, "carpeta", lambda: tmp_path)
    of = _contrato(con, obra, proveedor)
    CP.guardar_condiciones(con, of, {"ret_pct": "5", "isp": 1, "email": "admin@subcontratasprueba.es"}, "t")
    r = CP.guardar_lineas(con, of, [{"codigo": "A1", "descripcion": "Fábrica de ladrillo", "unidad": "m2", "cantidad": "1000", "precio": "8,50",
                                     "cert_partida": "05.01"},
                                    {"codigo": "A2", "descripcion": "Tabique", "unidad": "m2", "cantidad": "500", "precio": "3", "cert_partida": "05.02"}], "t")
    assert r["total_cents"] == 1_000_000 and r["diferencia_cents"] == 0
    cid = CP.nueva(con, of, "2026-09", "2026-09-30", "jefe")
    lin = {l["codigo"]: l["id"] for l in CP.lineas(con, of)}
    av = CP.medir(con, cid, {lin["A1"]: "400", lin["A2"]: "100"}, "jefe")
    assert not av
    c = db.one(con, "SELECT * FROM certs_proveedor WHERE id=?", (cid,))
    assert c["base_mes_cents"] == 400 * 850 + 100 * 300 and c["iva_cents"] == 0            # ISP
    assert c["ret_cents"] == round(c["base_mes_cents"] * 0.05) and c["liquido_cents"] == c["base_mes_cents"] - c["ret_cents"]
    with pytest.raises(ValueError):
        CP.aprobar(con, cid, "jefe", "gestor")                                           # cuatro ojos
    mid = CP.aprobar(con, cid, "ana", "gestor")
    c = db.one(con, "SELECT * FROM certs_proveedor WHERE id=?", (cid,))
    assert c["estado"] == "aprobada" and c["pdf_path"] and mid
    import pdfplumber
    texto = pdfplumber.open(c["pdf_path"]).pages[0].extract_text()
    assert "AUTORIZACIÓN DE FACTURACIÓN" in texto and "3.700,00" in texto
    assert db.one(con, "SELECT tipo, adjuntos FROM correo_salida WHERE id=?", (mid,))["tipo"] == "autorizacion_facturacion"
    # coste devengado sin factura
    dev = obra_control.devengado_pendiente(con, obra)
    assert any(d["tipo"] == "cert_propia" and d["base_c"] == 370000 for d in dev)
    # siguiente mes: parte de lo aprobado; exceso exige orden de cambio
    cid2 = CP.nueva(con, of, "2026-10", "2026-10-31", "jefe")
    assert Decimal(db.one(con, "SELECT cant_anterior FROM cert_prov_lineas WHERE cert_id=? AND linea_id=?", (cid2, lin["A1"]))["cant_anterior"]) == 400
    av = CP.medir(con, cid2, {lin["A1"]: "1050"}, "jefe")
    assert any(a.startswith("EXCESO") for a in av)
    with pytest.raises(ValueError):
        CP.aprobar(con, cid2, "ana", "gestor")
    CP.aprobar(con, cid2, "ana", "gestor", exceso_referencia="OC-7 aprobada por la propiedad")
    # llega la factura del mes de septiembre: casa sola y la certificación pasa a facturada
    did, _ = ingesta.registrar_pdf(con, "f.pdf", b"%PDF-1.4 factura subcontrata" * 20, "t")
    con.execute("""UPDATE documentos SET tipo_documento='factura', estado='pendiente_revision', proveedor_id=?, obra_id=?, numero='S-1',
                   fecha='2026-10-05', base_imponible_cents=370000 WHERE id=?""", (proveedor, obra, did))
    con.commit()
    assert CP.casar_facturas(con, obra) == 1
    assert db.one(con, "SELECT estado FROM certs_proveedor WHERE id=?", (cid,))["estado"] == "facturada"
    cas = [x for x in casacion.casar(con, obra) if x["id"] == did][0]
    assert cas["casada"] and "(propia)" in cas["certificacion"]
    with pytest.raises(ValueError):
        CP.anular(con, cid, "ana", "gestor", "error")


def test_lineas_desde_excel():
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame({"Código": ["X1"], "Descripción": ["Solado"], "Ud": ["m2"], "Medición": ["120,5"], "Precio unitario": ["14,20"],
                  "Partida cliente": ["11.02"]}).to_excel(buf, index=False)
    f = CP.leer_excel_lineas(buf.getvalue(), "c.xlsx")
    assert f[0]["cantidad"] == "120,5" and f[0]["cert_partida"] == "11.02"


# ============================================================================ certificación al cliente
def test_propuesta_certificacion_cliente(con, obra, proveedor, tmp_path, monkeypatch):
    monkeypatch.setattr(CP, "carpeta", lambda: tmp_path)
    base = _cert_cliente(con, obra)
    of = _contrato(con, obra, proveedor)
    CP.guardar_lineas(con, of, [{"codigo": "A1", "descripcion": "Fábrica", "unidad": "m2", "cantidad": "1000", "precio": "8.5", "cert_partida": "05.01"},
                                {"codigo": "A2", "descripcion": "Tabique", "unidad": "ml", "cantidad": "200", "precio": "3", "cert_partida": "05.02"}], "t")
    cid = CP.nueva(con, of, "2026-09", "2026-09-30", "jefe")
    lin = {l["codigo"]: l["id"] for l in CP.lineas(con, of)}
    CP.medir(con, cid, {lin["A1"]: "400", lin["A2"]: "50"}, "jefe")
    CP.aprobar(con, cid, "ana", "gestor")
    p = CC.proponer(con, obra, "2026-09")
    l = {x["codigo"]: x for x in p["lineas"]}
    assert l["05.01"]["cant_origen"] == Decimal("400")                                  # misma unidad: cantidad directa
    assert l["05.02"]["cant_origen"] == Decimal("125.000")                              # 25 % de avance × 500 de presupuesto
    assert p["totales"]["mes"] == Decimal("400") * 30 + Decimal("125") * 12
    assert p["totales"]["coste_proveedores_mes"] == Decimal("3550")
    # ajuste manual guardado como borrador
    CC.guardar_borrador(con, obra, "2026-09", base, {l["05.02"]["id"]: "130"}, "ana")
    b = CC.cargar_borrador(con, obra, "2026-09")
    p2 = CC.proponer(con, obra, "2026-09", base, b["ajustes"])
    assert {x["codigo"]: x for x in p2["lineas"]}["05.02"]["cant_origen"] == Decimal("130")
    assert CC.excel(p2)[:2] == b"PK"


# ============================================================================ planificación
def test_camino_critico_y_replanificacion(con, obra):
    PL.guardar_config(con, obra, "2026-09-07", "2026-10-12", False, None, "t")            # lunes; 12-oct festivo
    PL.guardar_actividades(con, obra, [
        {"codigo": "A", "nombre": "Replanteo", "duracion": 2},
        {"codigo": "B", "nombre": "Cimentación", "duracion": 10, "predecesoras": "A"},
        {"codigo": "C", "nombre": "Acopios", "duracion": 3, "predecesoras": "A"},
        {"codigo": "D", "nombre": "Estructura", "duracion": 20, "predecesoras": "B; C"},
        {"codigo": "E", "nombre": "Instalaciones", "duracion": 5, "predecesoras": "D CC+5"},
    ], "t")
    p = PL.planificar(con, obra)
    a = {x["codigo"]: x for x in p["actividades"]}
    assert a["A"]["inicio"] == date(2026, 9, 7) and a["B"]["inicio"] == date(2026, 9, 9)
    assert a["B"]["critica"] and a["D"]["critica"] and not a["C"]["critica"] and a["C"]["holgura"] == 7
    assert a["E"]["es"] == a["D"]["es"] + 5
    assert p["duracion_total"] == 32
    PL.congelar_linea_base(con, obra, "Planning inicial", "t")
    # la cimentación empezó tarde y va al 50 % con 8 días por delante a la fecha de control
    acts = PL.actividades(con, obra)
    for x in acts:
        if x["codigo"] == "A":
            x.update(inicio_real="2026-09-07", fin_real="2026-09-08")
        if x["codigo"] == "B":
            x.update(inicio_real="2026-09-14", avance=50, restante=8)
    PL.guardar_actividades(con, obra, acts, "t")
    PL.guardar_config(con, obra, "2026-09-07", "2026-10-12", False, "2026-09-21", "t")
    r = PL.replanificar(con, obra)
    assert r["retraso_fin_dias"] > 0
    movidas = {c["codigo"] for c in r["cambios"]}
    assert {"B", "D", "E"} <= movidas and "A" not in movidas


def test_ciclo_detectado(con, obra):
    with pytest.raises(ValueError, match="circulares"):
        PL.guardar_actividades(con, obra, [{"codigo": "A", "nombre": "a", "duracion": 1, "predecesoras": "B"},
                                           {"codigo": "B", "nombre": "b", "duracion": 1, "predecesoras": "A"}], "t")
    assert PL.parse_predecesoras("A10; A20CC+2; A30FF-1; A40FS") == [("A10", "FC", 0), ("A20", "CC", 2), ("A30", "FF", -1), ("A40", "FC", 0)]


# ============================================================================ planos
def test_versiones_de_planos(con, obra, tmp_path, monkeypatch):
    monkeypatch.setattr(PN, "carpeta", lambda o: tmp_path)
    assert PN.proponer_desde_nombre("ARQ-101_R03.pdf") == ("ARQ-101", "03")
    assert PN.proponer_desde_nombre("EST 12 rev B.pdf") == ("EST 12", "B")
    assert PN.proponer_desde_nombre("Planta baja.pdf") == ("Planta baja", None)
    d = PN.crear_documento(con, obra, "ARQ-101", "Planta baja", "plano", "Arquitectura", "", "t")
    r0 = PN.subir_revision(con, d, "00", "ARQ-101_R00.pdf", b"%PDF r0", "t")
    with pytest.raises(ValueError):
        PN.subir_revision(con, d, "01", "copia.pdf", b"%PDF r0", "t")                    # mismo archivo
    PN.enviar_df(con, r0, "t")
    PN.resolver_df(con, r0, "aprobada", "Arq. García (DF)", "", "t")
    r1 = PN.subir_revision(con, d, "01", "ARQ-101_R01.pdf", b"%PDF r1", "t")
    PN.distribuir(con, r0, ["Subcontratas Prueba SL"], "papel", "t")
    PN.enviar_df(con, r1, "t")
    l = PN.listado(con, obra)[0]
    assert l["vigente"] == "00" and l["ultima"] == "01" and "sin aprobar" in l["aviso"]
    with pytest.raises(ValueError):
        PN.subir_revision(con, d, "00", "x.pdf", b"%PDF otro", "t")                      # revisión repetida
    PN.resolver_df(con, r1, "aprobada_comentarios", "Arq. García (DF)", "Cotas de escalera a revisar", "t")
    assert PN.vigente(con, d)["revision"] == "01"
    assert db.one(con, "SELECT estado FROM planos_revisiones WHERE id=?", (r0,))["estado"] == "superada"
    assert len(PN.entregas_a_retirar(con, obra)) == 1


# ============================================================================ seguridad y salud
def test_cae_acceso_y_residuos(con, obra, proveedor):
    PV.alta_empresa(con, obra, proveedor, "2026-09-01", "t", email="prl@subcontratasprueba.es")
    tid = PV.alta_trabajador(con, proveedor, "Juan Pérez", "12345678Z", "Oficial 1ª", "t")
    PV.alta_trabajador_obra(con, tid, obra, "2026-09-01", "t")
    r = PV.puede_acceder(con, obra, "12345678z", date(2026, 9, 15), registrar=True, usuario="garita")
    assert not r["permitido"] and any("AEAT" in m for m in r["motivos"])
    for q in PV.requisitos(con):
        if q["obligatorio"]:
            kw = {"proveedor_id": proveedor} if q["ambito"] == "empresa" else {"trabajador_id": tid}
            did = PV.subir_documento(con, q["id"], "t", fecha_emision="2026-09-01", nombre="d.pdf", data=b"%PDF doc " + str(q["id"]).encode(), **kw)
            PV.validar_documento(con, did, "prl", True)
    r = PV.puede_acceder(con, obra, "12345678Z", date(2026, 9, 15))
    assert r["permitido"], r["motivos"]
    # la SS caduca al mes: en noviembre ya no entra
    r = PV.puede_acceder(con, obra, "12345678Z", date(2026, 11, 15))
    assert not r["permitido"] and any("Seguridad Social" in m for m in r["motivos"])
    assert db.one(con, "SELECT COUNT(*) n FROM cae_accesos")["n"] == 1
    with pytest.raises(ValueError):
        PV.alta_empresa(con, obra, proveedor, "2026-09-02", "t", nivel=4)
    # residuos: albarán → factura por nº de albarán
    did, _ = ingesta.registrar_pdf(con, "gestor.pdf", b"%PDF-1.4 gestor residuos" * 20, "t")
    con.execute("UPDATE documentos SET tipo_documento='factura', estado='aprobada', obra_id=?, albaranes='ALB-00123, ALB-124' WHERE id=?", (obra, did))
    con.commit()
    rid = PV.registrar_retirada(con, obra, {"ler": "17 09 04", "cantidad": "7,5", "albaran": "alb 123", "gestor": "Gestor Residuos SL"}, "t")
    ret = PV.retiradas(con, obra)[0]
    assert ret["documento_id"] == did and ret["trazabilidad"] == "Falta: certificado del gestor"
    PV.completar_retirada(con, rid, "t", certificado="CERT-9", certificado_fecha="2026-09-30")
    assert PV.retiradas(con, obra)[0]["trazabilidad"] == "Completa"
    PV.guardar_previstos(con, obra, [{"ler": "170904", "cantidad": "10", "unidad": "t"}], "t")
    assert PV.resumen_ler(con, obra)[0]["desviacion_pct"] == -25.0
    with pytest.raises(ValueError):
        PV.registrar_retirada(con, obra, {"ler": "1234", "cantidad": "1"}, "t")


# ============================================================================ conciliación bancaria
def _remesa(con, obra, proveedor):
    db.set_setting(con, "sepa_nombre", "LLORCA GROUP HISPANIA SL")
    db.set_setting(con, "sepa_iban", "ES9121000418450200051332")
    did, _ = ingesta.registrar_pdf(con, "p.pdf", b"%PDF-1.4 pago" * 30, "t")
    con.execute("""UPDATE documentos SET tipo_documento='factura', estado='aprobada', proveedor_id=?, obra_id=?, numero='P-77', fecha='2026-09-01',
                   total_a_pagar_cents=250000, iban='ES7921000813610123456789' WHERE id=?""", (proveedor, obra, did))
    con.commit()
    rid, _ = pagos.generar(con, [did], "2026-09-25", "t", "gestor")
    return rid, did


def test_norma43_y_conciliacion(con, obra, proveedor):
    rid, did = _remesa(con, obra, proveedor)
    n43 = BC.generar_norma43_prueba([("2026-09-26", -250000, "TRANSF SEPA REMESA"), ("2026-09-27", -1250, "COMISION MANTENIMIENTO")],
                                    saldo_inicial=1_000_000)
    ext = BC.leer_norma43(n43)
    assert ext["cuentas"][0]["cuadra"] and len(ext["cuentas"][0]["movs"]) == 2
    r = BC.importar(con, n43, "extracto.n43", "t")
    assert r["nuevos"] == 2 and r["aplicados"] == 1                                    # la remesa se confirma sola
    assert db.one(con, "SELECT estado FROM remesas WHERE id=?", (rid,))["estado"] == "pagada"
    assert db.one(con, "SELECT pagada, fecha_pago FROM documentos WHERE id=?", (did,))["fecha_pago"] == "2026-09-26"
    assert BC.importar(con, n43, "extracto.n43", "t")["nuevos"] == 0                   # extracto repetido
    pend = BC.pendientes(con)
    assert len(pend) == 1 and not pend[0]["candidatos"]
    BC.marcar_otro(con, pend[0]["id"], "Comisión bancaria", "t")
    # deshacer devuelve la remesa a pendiente de confirmar
    m = db.one(con, "SELECT id FROM banco_movimientos WHERE tipo='remesa'")
    BC.deshacer(con, m["id"], "t")
    assert db.one(con, "SELECT estado FROM remesas WHERE id=?", (rid,))["estado"] == "generada"


def test_norma43_descuadrado():
    n43 = BC.generar_norma43_prueba([("2026-09-26", -1000, "X")], saldo_inicial=0).replace(b"00000000000001000", b"00000000000002000", 1)
    ext = BC.leer_norma43(n43)
    assert not ext["cuentas"][0]["cuadra"] and ext["avisos"]


def test_cobro_desde_excel(con, obra):
    cert = _cert_cliente(con, obra)
    con.execute("UPDATE certificaciones SET total_actual_m=10000000 WHERE id=?", (cert,))
    fid = tesoreria.desde_certificacion(con, cert, "F1", Decimal("10"), Decimal("5"), "2026-09-05", "t")
    con.execute("UPDATE facturas_emitidas SET cliente_nif='A58818501' WHERE id=?", (fid,))
    con.commit()
    codigo = tesoreria.emitir(con, fid, "t", "gestor")
    f = tesoreria.estado_factura(con, fid)
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame({"Fecha operación": ["30/09/2026"], "Concepto": [f"TRANSFERENCIA DE CLIENTE PRUEBA SA FRA {codigo}"],
                  "Importe": [f"{f['pendiente_c'] / 100:.2f}".replace(".", ",")], "Saldo": ["1.000,00"]}).to_excel(buf, index=False)
    r = BC.importar(con, buf.getvalue(), "banco.xlsx", "t")
    assert r["aplicados"] == 1
    assert tesoreria.estado_factura(con, fid)["pendiente_c"] == 0


# ============================================================================ actas desde audio
def test_actas_audio_con_transcriptor_simulado(con, obra, tmp_path, monkeypatch):
    monkeypatch.setattr(AA, "carpeta", lambda: tmp_path)
    monkeypatch.setattr(AA, "motor_disponible", lambda: ("simulado", "ok"))
    monkeypatch.setattr(AA, "transcribir_archivo", lambda ruta, modelo="small", idioma="es": {
        "segmentos": [{"inicio": 0, "fin": 5, "texto": "Buenos días, empezamos la reunión."},
                      {"inicio": 5, "fin": 12, "texto": "Pinturas Ortolá se compromete a entregar las muestras antes del 15/10/2026."},
                      {"inicio": 12, "fin": 20, "texto": "La propiedad pide cambiar el solado del ático, es un extra de unos 3.500 €."}],
        "texto": "…", "duracion": 20.0, "idioma": "es", "motor": "simulado"})
    db.set_setting(con, "actas_resumen_ia", "0")
    aid = AA.subir(con, obra, "Reunión semanal", "2026-09-29", "JO, DF, propiedad", "reunion.m4a", b"\x00" * 5000, "t")
    with pytest.raises(ValueError):
        AA.subir(con, obra, "otra", "2026-09-29", "", "reunion.m4a", b"\x00" * 5000, "t")
    assert "Transcrita" in AA.procesar_pendiente(con)
    a = db.one(con, "SELECT * FROM actas_audio WHERE id=?", (aid,))
    assert a["estado"] == "transcrita" and "[00:05]" in AA.texto_con_tiempos(a)
    prop = AA.propuestas(a)
    tipos = {p["tipo"] for p in prop}
    assert {"compromiso", "extra"} <= tipos
    assert any(p["fecha_limite"] == "2026-10-15" for p in prop) and any(p["importe"] == "3.500" for p in prop)
    rid = AA.crear_reunion(con, aid, obra, "2026-09-29", "Reunión semanal", "JO", AA.texto_con_tiempos(a), prop, "t")
    assert db.one(con, "SELECT COUNT(*) n FROM obra_compromisos WHERE reunion_id=?", (rid,))["n"] == len(prop)
    with pytest.raises(ValueError):
        AA.crear_reunion(con, aid, obra, "2026-09-29", "x", "", "", [], "t")
