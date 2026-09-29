"""Pruebas automáticas (pytest). Cada prueba trabaja sobre una base de datos nueva en una carpeta temporal."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["LLORCA_DATA_DIR"] = tempfile.mkdtemp(prefix="llorca_pytest_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

# Los scripts históricos (se ejecutan al importarse y comparten una misma base de datos) se lanzan aparte,
# cada uno en su propio proceso, desde test_scripts_reales.py.
collect_ignore = ["test_core.py", "test_obra.py", "test_auditoria.py", "casos_reales.py"]


@pytest.fixture
def con(tmp_path):
    from core import db, esquema
    c = db.connect(tmp_path / "prueba.db")
    esquema.inicializar(c)
    yield c
    c.close()


@pytest.fixture
def obra(con):
    from core import maestros
    return maestros.crear_obra(con, "901", "Residencial Prueba", cliente="Cliente Prueba SA")


@pytest.fixture
def proveedor(con):
    from core import maestros
    return maestros.upsert_proveedor(con, "B12345674", "Subcontratas Prueba SL")
