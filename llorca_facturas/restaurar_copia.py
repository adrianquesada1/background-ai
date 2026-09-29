"""
Restaura la base de datos desde una copia de seguridad (.db.gz). EJECUTAR CON LA APLICACIÓN PARADA.

    python restaurar_copia.py D:\\CopiasLlorca\\bd\\llorca_20260929_213000.db.gz
    python restaurar_copia.py <copia.db.gz> --archivos D:\\CopiasLlorca\\archivos     (recupera también PDFs y justificantes)

Antes de sustituir nada se comprueba la copia (huella SHA-256, integridad y recuentos) y la base actual se guarda al lado
con el sufijo «antes_de_restaurar».
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import respaldo  # noqa: E402
from core.config import DB_PATH, DATA_DIR  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Restaurar una copia de seguridad de Llorca · Control económico de obra")
    ap.add_argument("copia", help="archivo llorca_AAAAMMDD_HHMMSS.db.gz")
    ap.add_argument("--archivos", help="carpeta «archivos» de la copia, para recuperar PDFs y justificantes")
    ap.add_argument("--si", action="store_true", help="no pedir confirmación")
    a = ap.parse_args()
    ok, det = respaldo.simulacro(a.copia)
    print(("Copia válida: " if ok else "COPIA NO VÁLIDA: ") + det)
    if not ok:
        return 1
    if not a.si and input(f"Se sustituirá {DB_PATH}. ¿Seguro? (escriba SI): ").strip().upper() != "SI":
        print("Cancelado.")
        return 1
    previa = respaldo.restaurar(a.copia, DB_PATH)
    print(f"Base de datos restaurada. La anterior se ha guardado en {previa}")
    if a.archivos:
        n = 0
        for src in Path(a.archivos).rglob("*"):
            if src.is_file():
                dst = DATA_DIR / src.relative_to(a.archivos)
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    n += 1
        print(f"{n} archivo(s) recuperados en {DATA_DIR}")
    print("Ya puede arrancar la aplicación.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
