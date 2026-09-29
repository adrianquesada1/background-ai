"""Reproduce la hoja de auditoría de agosto del departamento a partir de los datos brutos."""
import os, sys, tempfile
os.environ.setdefault("LLORCA_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from decimal import Decimal
from pathlib import Path
from core import db, maestros, obra_control as oc, auditoria as A, certificacion as C

DEMO = Path(__file__).resolve().parent.parent / "demo"
con = db.connect(); db.init_db(con); oc.init(con); A.init(con); maestros.seed_inicial(con)
c = C.leer_pdf((DEMO / "Certificacion_21.pdf").read_bytes())
cid = oc.guardar_certificacion(con, c, None, "cert21", 1, "test")
res = A.importar_libro(con, 1, (DEMO / "ALIBUILDING_AGOSTO_CRITERIOS_JULIO.xlsx").read_bytes(), "hoja", "test", cid)
r = A.calcular(con, 1, cid)
esperado = {"ventas": "6200595.52", "compras": "4487950.09", "rrhh": "387446.00", "resultado_sis": "1325199.43",
            "correccion": "-901274.76", "resultado": "423924.67"}
for k, v in esperado.items():
    assert A.r2(r[k]) == Decimal(v), (k, A.r2(r[k]), v)
assert r["partidas_sin_criterio"] == 0 and not r["proveedores_sin_categoria"]
pr = A.proyeccion(r)
print("OK · resultado", A.r2(r["resultado"]), "· proyección", A.r2(pr["proyeccion"]), f"({A.r2(pr['pct'])} %)")
print("avisos:", res["avisos"])
