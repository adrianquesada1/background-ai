"""Humo de las pantallas: cada página nueva (y las tocadas) se dibuja sin errores, vacía y con datos, con varios cargos."""
import json
import os
from pathlib import Path

import pytest

VISTAS = ["buzon", "correo_salida", "sis_envios", "conciliacion", "automatizaciones", "cert_proveedor", "cert_cliente", "planificacion", "planos",
          "prevencion", "actas", "pagos", "tesoreria", "proveedores", "estudios", "cierre", "ajustes", "inicio"]
RAIZ = str(Path(__file__).resolve().parent.parent)


def _script():
    import importlib
    import os
    import sys
    sys.path.insert(0, os.environ["LLORCA_RAIZ"])
    import streamlit as st
    st.session_state.setdefault("auth", {"id": 1, "usuario": "admin", "nombre": "Admin", "rol": os.environ["LLORCA_ROL"]})
    importlib.import_module("vistas." + os.environ["LLORCA_VISTA"]).render()


@pytest.fixture(scope="module")
def base_global():
    """Base de datos de la aplicación (la de LLORCA_DATA_DIR) con datos de ejemplo de todos los módulos."""
    from core import db, esquema, usuarios, maestros, obra_control, cert_proveedor as CP, planificacion as PL, planos as PN, prevencion as PV
    from core import conciliacion as BC, buzon, correo
    from tests.test_buzon import _correo, _factura, _presupuesto
    c = db.connect()
    esquema.inicializar(c)
    db.set_setting(c, "respaldo_activo", "0")
    db.set_setting(c, "buzon_leer_auto", "0")
    if not db.one(c, "SELECT id FROM usuarios WHERE usuario='admin'"):
        usuarios.crear(c, "admin", "Admin", "Clave-Segura-2026!", "admin", "t")
    obra = maestros.crear_obra(c, "902", "Pantallas", cliente="Cliente SA")
    prov = maestros.upsert_proveedor(c, "B12345674", "Subcontratas Prueba SL")
    c.commit()
    of = obra_control.guardar_oferta(c, {"obra_id": obra, "proveedor_id": prov, "alcance": "Albañilería", "importe_cents": 1_000_000,
                                         "estado": "adjudicada"}, "t")
    CP.guardar_lineas(c, of, [{"codigo": "A1", "descripcion": "Fábrica", "unidad": "m2", "cantidad": "1000", "precio": "10"}], "t")
    cid = CP.nueva(c, of, "2026-09", "2026-09-30", "jefe")
    CP.medir(c, cid, {CP.lineas(c, of)[0]["id"]: "1200"}, "jefe")
    PL.guardar_config(c, obra, "2026-09-07", "", False, "2026-09-21", "t")
    PL.guardar_actividades(c, obra, [{"codigo": "A", "nombre": "Replanteo", "duracion": 2},
                                     {"codigo": "B", "nombre": "Cimentación", "duracion": 10, "predecesoras": "A"}], "t")
    PL.congelar_linea_base(c, obra, "Base", "t")
    d = PN.crear_documento(c, obra, "ARQ-1", "Planta", "plano", "Arquitectura", "", "t")
    PN.enviar_df(c, PN.subir_revision(c, d, "00", "a.pdf", b"%PDF pantallas", "t"), "t")
    PV.alta_empresa(c, obra, prov, "2026-09-01", "t")
    PV.registrar_retirada(c, obra, {"ler": "170904", "cantidad": "3", "albaran": "7"}, "t")
    BC.importar(c, BC.generar_norma43_prueba([("2026-09-26", -1000, "COMISION")]), "e.n43", "t")
    buzon.procesar_mensaje(c, _correo([("f.pdf", _factura("PANT-1")), ("p.pdf", _presupuesto())], mid="<pantallas@p>"), "1", "INBOX")
    correo.preparar(c, "otro", "a@b.es", "Prueba", "Texto", "t")
    c.execute("INSERT INTO actas_audio (obra_id, titulo, fecha, estado, transcripcion, segmentos, sha256) VALUES (?,?,?,?,?,?,?)",
              (obra, "Reunión", "2026-09-29", "transcrita", "x", json.dumps([{"inicio": 0, "fin": 3, "texto": "Se acuerda revisar el replanteo."}]), "pant"))
    c.commit()
    yield obra
    c.close()


@pytest.mark.parametrize("rol", ["admin", "jefe_obra", "consulta"])
@pytest.mark.parametrize("vista", VISTAS)
def test_pantalla_sin_errores(base_global, vista, rol):
    from streamlit.testing.v1 import AppTest
    os.environ.update({"LLORCA_RAIZ": RAIZ, "LLORCA_VISTA": vista, "LLORCA_ROL": rol})
    at = AppTest.from_function(_script, default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
