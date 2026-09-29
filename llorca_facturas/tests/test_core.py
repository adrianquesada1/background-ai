import os, sys, tempfile
os.environ["LLORCA_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from core import db, maestros, ingesta, analytics, validation, export, agente
from core.money import fmt_eur, from_cents
from tests.casos_reales import CASOS

SRC = Path(os.environ.get("FACTURAS_DIR", Path(__file__).resolve().parent.parent / "demo"))
con = db.connect(); db.init_db(con); maestros.seed_inicial(con)
ids = {}
for fn, data in CASOS.items():
    did, nuevo = ingesta.registrar_pdf(con, fn, (SRC / fn).read_bytes(), "test")
    ingesta.aplicar_extraccion(con, did, {"data": data, "modelo": "manual"}, "test")
    ids[fn] = did
# re-subida del mismo PDF -> no duplica
did2, nuevo2 = ingesta.registrar_pdf(con, "x.pdf", (SRC / "90-26.pdf").read_bytes(), "test")
assert not nuevo2 and did2 == ids["90-26.pdf"], "hash dedupe falla"

for fn, did in ids.items():
    inc = db.rows(con, "SELECT severidad, codigo, mensaje FROM incidencias WHERE documento_id=?", (did,))
    d = db.one(con, "SELECT obra_id, obra_confianza, tiene_texto FROM documentos WHERE id=?", (did,))
    print(f"\n#{did} {fn}  obra={d['obra_id']} conf={d['obra_confianza']} texto={d['tiene_texto']}")
    for i in inc: print("   ", i["severidad"], i["codigo"], i["mensaje"])

ok, a, b = analytics.comprobar_invariante(con)
print("\nINVARIANTE", ok, a, b)
r = analytics.resumen(con, obra_id=1)
print({k: (fmt_eur(v) if hasattr(v,'quantize') else v) for k,v in r.items()})
pp = analytics.por_partida(con, 1)
print(pp[pp.coste_c!=0][["codigo","descripcion","coste_c","n_lineas","extras_c"]])
print(analytics.por_proveedor(con)[["proveedor","n_docs","base_c","peso_pct","acumulado_pct"]])
print(analytics.mensual(con))
print(analytics.retenciones(con)[["proveedor","numero","ret_c","liberacion_estimada"]])

# duplicado simulado y cambio de IBAN
data = dict(CASOS["90-26.pdf"]); 
did3, _ = ingesta.registrar_pdf(con, "dup.pdf", (SRC/"90-26.pdf").read_bytes() + b"\n% copia reenviada por el proveedor\n", "test")
ingesta.aplicar_extraccion(con, did3, {"data": data, "modelo": "manual"}, "test")
print("\nDUP:", [ (i["severidad"], i["mensaje"]) for i in db.rows(con,"SELECT * FROM incidencias WHERE documento_id=? AND codigo IN ('C12','C16')",(did3,))])
ingesta.guardar_cabecera(con, did3, {"iban": "ES91 2100 0418 4502 0005 1332", "fecha": "2026-09-20"}, "test")
print("IBAN:", [ (i["severidad"], i["mensaje"]) for i in db.rows(con,"SELECT * FROM incidencias WHERE documento_id=? AND codigo='C11'",(did3,))])
okap, msg = ingesta.cambiar_estado(con, did3, "aprobada", "test"); print("aprobar bloqueado:", not okap)
okap, msg = ingesta.cambiar_estado(con, ids["INV-26086.pdf"], "aprobada", "test"); print("aprobar INV-26086:", okap, msg[:80])
ingesta.cambiar_estado(con, did3, "rechazada", "test")

# herramientas del agente
for t, a in [("resumen_obra", {"obra": "664"}), ("buscar_lineas", {"texto": "papel", "obra": "664"}),
             ("coste_por_proveedor", {"obra": "alibuilding"}), ("pagos_pendientes", {}), ("incidencias_abiertas", {"severidad":"alta"})]:
    out = agente.ejecutar_herramienta(con, t, a)
    import json; print("\nTOOL", t, json.dumps(out, ensure_ascii=False, default=str)[:500])
x = export.exportar_excel(con, obra_id=1); print("\nXLSX bytes", len(x))
open("/tmp/test.xlsx","wb").write(x)
