import os, sys, tempfile, copy
os.environ.setdefault("LLORCA_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from decimal import Decimal
from pathlib import Path
from core import db, maestros, ingesta, obra_control as oc, certificacion as C
from core.money import from_cents
from tests.casos_reales import CASOS
DEMO = Path(__file__).resolve().parent.parent / "demo"
CERT = Path(os.environ.get("CERT_PDF", DEMO / "Certificacion_21.pdf"))
con = db.connect(); db.init_db(con); oc.init(con); maestros.seed_inicial(con)
for fn, d in CASOS.items():
    did, _ = ingesta.registrar_pdf(con, fn, (DEMO / fn).read_bytes(), "t")
    ingesta.aplicar_extraccion(con, did, {"data": d, "modelo": "manual"}, "t")
data = CERT.read_bytes()
c21 = C.leer_pdf(data)
assert not C.verificar(c21), C.verificar(c21)
obra = oc.buscar_obra_para_cert(con, c21); print("obra detectada", obra)
# certificación 20 simulada a partir de la 21 (anterior -> origen) con un cambio de precio
c20 = copy.deepcopy(c21); c20.numero = 20; c20.fecha = "2026-07-27"
for l in c20.lineas:
    l.imp_origen, l.cant_origen = l.imp_anterior, l.cant_anterior
    l.imp_actual = l.cant_actual = Decimal(0)
c20.lineas[0].precio = Decimal("1.300")
c20.totales = (sum(l.imp_origen for l in c20.lineas), Decimal(0), Decimal(0))
oc.guardar_certificacion(con, c20, None, "sim20", obra, "t")
cid = oc.guardar_certificacion(con, c21, data, CERT.name, obra, "t")
print("encadenado:", oc.comprobar_encadenado(con, obra)[:3])
cmp = oc.comparar(con, oc.certificaciones(con, obra)[0]["id"], cid)
print(cmp["cambio"].value_counts().to_dict())
print(oc.adoptar_estructura(con, obra, cid, "t"))
print([ (p["codigo"], p["descripcion"][:25]) for p in maestros.partidas_de_obra(con, obra)][:5])
# ofertas
p11 = db.one(con, "SELECT id FROM partidas WHERE obra_id=? AND codigo='11'", (obra,))["id"]
for prov, imp, est in (("NEWKER", 18000000, "descartada"), ("HR SKILL CONSTRUCCIONS SL", 15500000, "adjudicada"), ("Otro solador", 16900000, "recibida")):
    oc.guardar_oferta(con, {"obra_id": obra, "partida_id": p11, "proveedor_nombre": prov, "importe_cents": imp, "estado": est,
                            "valorado_por": "Pilar Sirvent", "puntuacion": 4}, "t")
print("comparativa", oc.comparativa_partida(con, obra, p11))
for modo in ("origen", "mes"):
    df = oc.rentabilidad(con, obra, cid, modo)
    print(modo, df.attrs["periodo"], "cert", oc.m2d(df["certificado_m"].sum()), "coste", oc.m2d(df["coste_m"].sum()),
          "margen", oc.m2d(df["margen_m"].sum()))
    print(df[df.coste_m != 0][["codigo", "capitulo", "certificado_m", "coste_m", "margen_pct", "contratado_m"]].to_string()[:900])
cert = db.one(con, "SELECT * FROM certificaciones WHERE id=?", (cid,))
print("cobertura", round(oc.cobertura(con, obra, cert), 3))
for a in oc.alertas_rentabilidad(df, oc.cobertura(con, obra, cert))[:6]: print("  ", a)
print(oc.serie_mensual(con, obra))
print(oc.sugerir_partida_cert(con, obra, "P.B.ANTID 60X60 RC LAKESTONE SAND X"))
print("relacionadas", oc.relacionar_automatico(con, obra, 0.6, "t"))
print(oc.precios_venta_vs_coste(con, obra)[["concepto_factura", "precio_coste", "partida_venta", "precio_venta", "cert_confianza"]].head(8).to_string())
