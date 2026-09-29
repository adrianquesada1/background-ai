"""Pruebas con los documentos reales de la obra 664 (no se publican en el repositorio: ver demo/LEEME.md).
Cada script histórico se ejecuta en su propio proceso y con su propia base de datos; si faltan los documentos, se omite."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
DEMO = RAIZ / "demo"
NECESITA = {"test_core.py": ["90-26.pdf", "../tests/casos_reales.py"], "test_obra.py": ["Certificacion_21.pdf", "../tests/casos_reales.py"],
            "test_auditoria.py": ["Certificacion_21.pdf", "ALIBUILDING_AGOSTO_CRITERIOS_JULIO.xlsx"]}


@pytest.mark.parametrize("script", sorted(NECESITA))
def test_script_con_documentos_reales(script):
    faltan = [f for f in NECESITA[script] if not (DEMO / f).exists()]
    if faltan:
        pytest.skip(f"Faltan documentos reales: {', '.join(faltan)}")
    env = {**os.environ, "LLORCA_DATA_DIR": tempfile.mkdtemp(prefix="llorca_real_")}
    r = subprocess.run([sys.executable, str(RAIZ / "tests" / script)], cwd=RAIZ, env=env, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-3000:]
