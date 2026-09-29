"""
Inicialización única de todas las tablas de la aplicación.

La usan la aplicación (vistas/comun.get_con) y las pruebas automáticas, para que ambas trabajen siempre con el mismo
esquema. Cada módulo crea sus tablas con CREATE TABLE IF NOT EXISTS y añade columnas nuevas con ALTER TABLE tolerante:
se puede llamar tantas veces como se quiera sobre una base de datos existente sin perder nada.
"""
from __future__ import annotations

from . import db


def inicializar(con) -> None:
    from . import (obra_control, auditoria, usuarios, sesiones, trabajos, ingesta, plantillas, historial, indice, gobierno,
                   internos, tesoreria, estudios, cierres, pagos, maestros)
    db.init_db(con)
    obra_control.init(con)
    auditoria.init(con)
    usuarios.init(con)
    sesiones.init(con)
    trabajos.init(con)
    ingesta._cols_extra(con)
    plantillas.init(con)
    historial.init(con)
    indice.init(con)
    gobierno.init(con)
    internos.init(con)
    tesoreria.init(con)
    estudios.init(con)
    cierres.init(con)
    pagos.init(con)
    # ---- módulos de la versión 2
    from . import (planificador, buzon, respaldo, sis, correo, cert_proveedor, cert_cliente, planificacion, planos, prevencion,
                   conciliacion, actas_audio)
    for m in (planificador, buzon, respaldo, sis, correo, cert_proveedor, cert_cliente, planificacion, planos, prevencion,
              conciliacion, actas_audio):
        m.init(con)
    con.commit()
    maestros.seed_inicial(con)
