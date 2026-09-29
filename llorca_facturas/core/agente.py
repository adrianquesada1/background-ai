"""
Asistente conversacional sobre facturas/obras.

Arquitectura anti-alucinación:
- El modelo NO tiene acceso libre a la base de datos ni hace cuentas.
- Solo puede llamar a herramientas deterministas (funciones Python) que calculan
  las cifras exactas en céntimos y devuelven los importes ya formateados.
- Se le exige citar las cifras de las herramientas y los nº de factura.
- Todas las llamadas a herramientas se muestran al usuario (trazabilidad).
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import db, analytics
from .money import from_cents, fmt_eur


def _e(c) -> str:
    return fmt_eur(from_cents(int(c or 0)))


def _resolver_obra(con, obra: str | None):
    if not obra:
        return None
    o = db.one(con, "SELECT * FROM obras WHERE codigo=?", (str(obra).strip(),))
    if o:
        return o
    like = f"%{obra.strip()}%"
    return db.one(con, "SELECT * FROM obras WHERE nombre LIKE ? OR alias LIKE ? ORDER BY id LIMIT 1", (like, like))


def _filtros(con, a: dict) -> tuple[dict, str | None]:
    f = {}
    if a.get("obra"):
        o = _resolver_obra(con, a["obra"])
        if not o:
            return {}, f"No existe ninguna obra que coincida con «{a['obra']}»."
        f["obra_id"] = o["id"]
    if a.get("fecha_desde"):
        f["desde"] = a["fecha_desde"]
    if a.get("fecha_hasta"):
        f["hasta"] = a["fecha_hasta"]
    if a.get("solo_aprobadas"):
        f["estados"] = ["aprobada"]
    return f, None


FILTROS = {
    "obra": {"type": "string", "description": "Código (p.ej. '664') o nombre de la obra."},
    "fecha_desde": {"type": "string", "description": "YYYY-MM-DD"},
    "fecha_hasta": {"type": "string", "description": "YYYY-MM-DD"},
    "solo_aprobadas": {"type": "boolean", "description": "Solo facturas aprobadas (por defecto incluye pendientes y revisadas)."},
}

TOOLS = [
    {"name": "listar_obras", "description": "Lista las obras dadas de alta con su coste acumulado.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "resumen_obra", "description": "KPIs de coste de una obra o de todas: nº docs, base, IVA, retenciones, pendiente de pago.",
     "input_schema": {"type": "object", "properties": FILTROS}},
    {"name": "coste_por_partida", "description": "Coste real por partida frente a presupuesto, desviación y extras.",
     "input_schema": {"type": "object", "properties": FILTROS, "required": ["obra"]}},
    {"name": "coste_por_proveedor", "description": "Ranking de proveedores/industriales por importe (Pareto).",
     "input_schema": {"type": "object", "properties": FILTROS}},
    {"name": "evolucion_mensual", "description": "Coste por mes y acumulado.",
     "input_schema": {"type": "object", "properties": FILTROS}},
    {"name": "buscar_documentos", "description": "Busca facturas por proveedor, número, concepto, importe o fechas.",
     "input_schema": {"type": "object", "properties": {
         **FILTROS,
         "texto": {"type": "string", "description": "Texto en proveedor, número, concepto o nombre de archivo."},
         "importe_min": {"type": "number"}, "importe_max": {"type": "number"},
         "estado": {"type": "string", "enum": ["pendiente_revision", "revisada", "aprobada", "rechazada"]},
         "limite": {"type": "integer", "default": 25}}}},
    {"name": "detalle_documento", "description": "Detalle completo de un documento: cabecera, líneas, IVA, incidencias.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "buscar_lineas", "description": "Suma y lista líneas cuyo concepto contiene un texto (p.ej. 'gres', 'papel pintado').",
     "input_schema": {"type": "object", "properties": {**FILTROS, "texto": {"type": "string"}}, "required": ["texto"]}},
    {"name": "incidencias_abiertas", "description": "Incidencias sin resolver (duplicados, descuadres, cambios de IBAN...).",
     "input_schema": {"type": "object", "properties": {
         "obra": {"type": "string"},
         "severidad": {"type": "string", "enum": ["critica", "alta", "media", "info"]}}}},
    {"name": "retenciones_garantia", "description": "Retenciones de garantía practicadas a proveedores y su liberación estimada.",
     "input_schema": {"type": "object", "properties": FILTROS}},
    {"name": "pagos_pendientes", "description": "Facturas pendientes de pago agrupadas por tramo de vencimiento.",
     "input_schema": {"type": "object", "properties": FILTROS}},
    {"name": "rentabilidad_capitulos", "description": "Venta certificada vs coste vs contratado y margen por capítulo de la "
     "última certificación (o la indicada). modo 'mes' = certificado del mes vs coste del periodo; 'origen' = acumulado.",
     "input_schema": {"type": "object", "properties": {"obra": {"type": "string"}, "modo": {"type": "string", "enum": ["mes", "origen"]},
                                                       "numero_certificacion": {"type": "integer"}}, "required": ["obra"]}},
    {"name": "certificacion_resumen", "description": "Datos de las certificaciones de una obra: nº, fecha, a origen, mes, "
     "órdenes de cambio, revisiones, y cambios frente a la anterior.",
     "input_schema": {"type": "object", "properties": {"obra": {"type": "string"}}, "required": ["obra"]}},
    {"name": "buscar_partidas_certificacion", "description": "Busca partidas de la certificación por texto (precio de venta, % a origen, importes).",
     "input_schema": {"type": "object", "properties": {"obra": {"type": "string"}, "texto": {"type": "string"}}, "required": ["obra", "texto"]}},
    {"name": "ofertas_contratos", "description": "Ofertas por capítulo: proveedores, importes, estado, quién valoró, opinión y valoración.",
     "input_schema": {"type": "object", "properties": {"obra": {"type": "string"}, "capitulo": {"type": "string"}}, "required": ["obra"]}},
    {"name": "auditoria_mensual", "description": "Auditoría mensual con el método del departamento: ventas, compras y personal "
     "a origen, resultado SIS, margen por categoría (cobrado sin pase vs pagado), corrección y proyección a fin de obra.",
     "input_schema": {"type": "object", "properties": {"obra": {"type": "string"}}, "required": ["obra"]}},
    {"name": "historico_precios", "description": "Precios unitarios históricos de un material o trabajo.",
     "input_schema": {"type": "object", "properties": {"texto": {"type": "string"}, "obra": {"type": "string"}},
                      "required": ["texto"]}},
]


def ejecutar_herramienta(con, nombre: str, a: dict) -> dict:
    if nombre == "listar_obras":
        out = []
        for o in db.rows(con, "SELECT * FROM obras ORDER BY codigo"):
            r = analytics.resumen(con, obra_id=o["id"])
            out.append({"codigo": o["codigo"], "nombre": o["nombre"], "cliente": o["cliente"],
                        "documentos": r.get("n_docs", 0), "coste_base": fmt_eur(r.get("base", 0)),
                        "presupuesto_coste": _e(o["presupuesto_coste_cents"])})
        return {"obras": out}

    f, err = _filtros(con, a)
    if err:
        return {"error": err}

    if nombre == "resumen_obra":
        r = analytics.resumen(con, **f)
        if not r.get("n_docs"):
            return {"resultado": "Sin documentos para esos filtros."}
        ok, a_c, b_c = analytics.comprobar_invariante(con, **f)
        return {k: (fmt_eur(v) if hasattr(v, "quantize") else v) for k, v in r.items()} | {
            "cuadre_partidas_vs_bases": "OK" if ok else f"DESCUADRE {_e(a_c - b_c)}"}

    if nombre == "coste_por_partida":
        df = analytics.por_partida(con, f.pop("obra_id"), **f)
        return {"partidas": [{"codigo": r.codigo, "partida": r.descripcion, "coste": _e(r.coste_c),
                              "presupuesto": _e(r.ppto_c) if r.ppto_c else "sin presupuesto",
                              "desviacion": _e(r.desviacion_c) if r.ppto_c else None,
                              "consumido_pct": r.consumido_pct, "extras": _e(r.extras_c),
                              "n_lineas": int(r.n_lineas)} for r in df.itertuples() if r.coste_c or r.ppto_c],
                "total": _e(df["coste_c"].sum())}

    if nombre == "coste_por_proveedor":
        df = analytics.por_proveedor(con, **f)
        if df.empty:
            return {"resultado": "Sin datos"}
        return {"proveedores": [{"proveedor": r.proveedor, "nif": r.nif, "documentos": int(r.n_docs),
                                 "base": _e(r.base_c), "peso_pct": float(r.peso_pct),
                                 "acumulado_pct": float(r.acumulado_pct), "ret_garantia": _e(r.ret_c),
                                 "pendiente_pago": _e(r.pendiente_c)} for r in df.itertuples()],
                "total": _e(df["base_c"].sum())}

    if nombre == "evolucion_mensual":
        df = analytics.mensual(con, **f)
        return {"meses": [{"mes": r.mes, "coste": _e(r.base_c), "acumulado": _e(r.acumulado_c)} for r in df.itertuples()]}

    if nombre == "buscar_documentos":
        df = analytics.documentos_df(con, **f) if a.get("estado") != "rechazada" else \
            analytics.documentos_df(con, estados=["rechazada"], **f)
        if df.empty:
            return {"resultado": "Sin coincidencias", "documentos": []}
        if a.get("estado") and a["estado"] != "rechazada":
            df = df[df["estado"] == a["estado"]]
        if a.get("texto"):
            t = a["texto"].lower()
            m = df.apply(lambda r: t in " ".join(str(r[c] or "") for c in ("proveedor", "numero", "concepto_general", "filename", "nif")).lower(), axis=1)
            df = df[m]
        if a.get("importe_min") is not None:
            df = df[df["base_c"] >= int(round(a["importe_min"] * 100))]
        if a.get("importe_max") is not None:
            df = df[df["base_c"] <= int(round(a["importe_max"] * 100))]
        lim = int(a.get("limite") or 25)
        return {"n_encontrados": len(df), "suma_base": _e(df["base_c"].sum()), "suma_a_pagar": _e(df["pagar_c"].sum()),
                "documentos": [{"id": int(r.id), "proveedor": r.proveedor, "numero": r.numero, "fecha": r.fecha,
                                "tipo": r.tipo_documento, "estado": r.estado, "base": _e(r.base_c), "a_pagar": _e(r.pagar_c),
                                "obra": r.obra_codigo, "concepto": r.concepto_general} for r in df.head(lim).itertuples()]}

    if nombre == "detalle_documento":
        d = db.one(con, "SELECT d.*, o.codigo AS obra_codigo FROM documentos d LEFT JOIN obras o ON o.id=d.obra_id WHERE d.id=?", (a["id"],))
        if not d:
            return {"error": "No existe"}
        lin = db.rows(con, "SELECT l.descripcion, l.cantidad, l.unidad, l.precio_unitario, l.importe_cents, p.codigo AS partida "
                           "FROM lineas l LEFT JOIN partidas p ON p.id=l.partida_id WHERE documento_id=? ORDER BY orden", (a["id"],))
        inc = db.rows(con, "SELECT severidad, mensaje, detalle, resuelta FROM incidencias WHERE documento_id=?", (a["id"],))
        return {"id": d["id"], "archivo": d["filename"], "tipo": d["tipo_documento"], "estado": d["estado"],
                "proveedor": d["emisor_nombre"], "nif": d["emisor_nif"], "numero": d["numero"], "fecha": d["fecha"],
                "obra": d["obra_codigo"], "base": _e(d["base_imponible_cents"]), "iva": _e(d["total_iva_cents"]),
                "total_factura": _e(d["total_factura_cents"]), "ret_garantia": _e(d["ret_garantia_cents"]),
                "irpf": _e(d["irpf_cents"]), "a_pagar": _e(d["total_a_pagar_cents"]),
                "inversion_sujeto_pasivo": bool(d["inversion_sujeto_pasivo"]),
                "lineas": [{**{k: v for k, v in l.items() if k != "importe_cents"}, "importe": _e(l["importe_cents"])} for l in lin[:80]],
                "incidencias": inc}

    if nombre == "buscar_lineas":
        lin = analytics.lineas_coste_df(con, **f)
        if lin.empty:
            return {"resultado": "Sin datos"}
        t = a["texto"].lower()
        sel = lin[lin["descripcion"].fillna("").str.lower().str.contains(t, regex=False)]
        por_prov = sel.groupby("proveedor")["importe_c"].sum().sort_values(ascending=False)
        return {"n_lineas": len(sel), "suma": _e(sel["importe_c"].sum()),
                "por_proveedor": {k: _e(v) for k, v in por_prov.items()},
                "lineas": [{"doc": int(r.documento_id), "numero": r.numero, "proveedor": r.proveedor, "fecha": r.fecha,
                            "descripcion": r.descripcion, "cantidad": r.cantidad, "precio": r.precio_unitario,
                            "importe": _e(r.importe_c), "partida": r.partida_codigo} for r in sel.head(40).itertuples()]}

    if nombre == "incidencias_abiertas":
        sql = ("SELECT i.severidad, i.mensaje, i.detalle, d.id AS doc, d.filename, d.numero, d.emisor_nombre "
               "FROM incidencias i JOIN documentos d ON d.id=i.documento_id LEFT JOIN obras o ON o.id=d.obra_id "
               "WHERE i.resuelta=0 AND d.estado<>'rechazada'")
        p = []
        if a.get("severidad"):
            sql += " AND i.severidad=?"; p.append(a["severidad"])
        if f.get("obra_id"):
            sql += " AND d.obra_id=?"; p.append(f["obra_id"])
        sql += " ORDER BY CASE i.severidad WHEN 'critica' THEN 0 WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END LIMIT 60"
        r = db.rows(con, sql, p)
        return {"n": len(r), "incidencias": r}

    if nombre == "retenciones_garantia":
        df = analytics.retenciones(con, **f)
        if df.empty:
            return {"resultado": "No hay retenciones de garantía registradas."}
        g = df.groupby("proveedor")["ret_c"].sum().sort_values(ascending=False)
        return {"total_retenido": _e(df["ret_c"].sum()),
                "pendiente_devolver": _e(df[df["ret_garantia_devuelta"] == 0]["ret_c"].sum()),
                "por_proveedor": {k: _e(v) for k, v in g.items()},
                "detalle": [{"proveedor": r.proveedor, "numero": r.numero, "fecha": r.fecha, "retencion": _e(r.ret_c),
                             "liberacion_estimada": r.liberacion_estimada} for r in df.itertuples()][:60]}

    if nombre == "pagos_pendientes":
        df = analytics.vencimientos(con, **f)
        if df.empty:
            return {"resultado": "No hay pagos pendientes."}
        g = df.groupby("tramo")["pagar_c"].agg(["sum", "size"])
        return {"hoy": date.today().isoformat(), "total_pendiente": _e(df["pagar_c"].sum()),
                "por_tramo": {k: {"importe": _e(v["sum"]), "facturas": int(v["size"])} for k, v in g.iterrows()},
                "vencidas": [{"proveedor": r.proveedor, "numero": r.numero, "vencimiento": r.fecha_vencimiento or r.fecha,
                              "a_pagar": _e(r.pagar_c)} for r in df[df["tramo"] == "Vencido"].itertuples()][:40]}

    if nombre in ("rentabilidad_capitulos", "certificacion_resumen", "buscar_partidas_certificacion", "ofertas_contratos"):
        from . import obra_control as oc
        o = _resolver_obra(con, a.get("obra"))
        if not o:
            return {"error": "Obra no encontrada"}
        certs = oc.certificaciones(con, o["id"])
        m = lambda v: fmt_eur(oc.m2d(int(v or 0)))  # noqa: E731
        if nombre == "ofertas_contratos":
            df = oc.ofertas_df(con, o["id"])
            if a.get("capitulo") and not df.empty:
                df = df[df["partida_codigo"] == str(a["capitulo"])]
            return {"ofertas": [{"capitulo": r.partida_codigo, "proveedor": r.proveedor, "estado": r.estado,
                                 "importe": _e(r.importe_cents), "valorado_por": r.valorado_por, "responsable": r.responsable,
                                 "valoracion": r.puntuacion, "opinion": r.opinion} for r in df.itertuples()] if not df.empty else []}
        if not certs:
            return {"error": "La obra no tiene certificaciones importadas"}
        if nombre == "certificacion_resumen":
            out = []
            for c in certs:
                caps = oc.cert_capitulos_df(con, c["id"])
                c1 = caps[caps["nivel"] == 1]
                out.append({"numero": c["numero"], "fecha": c["fecha"], "a_origen": m(c["total_origen_m"]),
                            "del_mes": m(c["total_actual_m"]),
                            "ordenes_cambio_origen": m(c1.loc[c1["tipo"] == "orden_cambio", "origen_m"].sum()),
                            "revisiones_y_opcionales_origen": m(c1.loc[c1["tipo"].isin(["modificacion", "opcional"]), "origen_m"].sum()),
                            "verificada": bool(c["verificada"])})
            cambios = {}
            if len(certs) > 1:
                cmp = oc.comparar(con, certs[-2]["id"], certs[-1]["id"])
                if not cmp.empty:
                    cambios = {k: {"n": int(v["n"]), "impacto": m(v["imp"])}
                               for k, v in cmp.groupby("cambio").agg(n=("codigo", "size"), imp=("impacto_m", "sum")).iterrows()}
            return {"certificaciones": out, "cambios_ultima_vs_anterior": cambios,
                    "incoherencias_encadenado": oc.comprobar_encadenado(con, o["id"])[:10]}
        if nombre == "buscar_partidas_certificacion":
            cl = oc.cert_lineas_df(con, certs[-1]["id"])
            t = a["texto"].lower()
            cl = cl[cl.apply(lambda r: t in f"{r['codigo']} {r['titulo']} {r['descripcion']}".lower(), axis=1)]
            return {"n": len(cl), "suma_origen": m(cl["origen_m"].sum()), "suma_mes": m(cl["actual_m"].sum()),
                    "partidas": [{"capitulo": r.capitulo, "codigo": r.codigo, "titulo": r.titulo, "unidad": r.unidad,
                                  "precio_venta": r.precio, "pct_origen": r.pct_origen, "a_origen": m(r.origen_m),
                                  "mes": m(r.actual_m)} for r in cl.head(40).itertuples()]}
        c = next((x for x in certs if x["numero"] == a.get("numero_certificacion")), certs[-1])
        df = oc.rentabilidad(con, o["id"], c["id"], a.get("modo") or "mes")
        cob = oc.cobertura(con, o["id"], c)
        return {"certificacion": c["numero"], "modo": a.get("modo") or "mes", "periodo_coste": df.attrs.get("periodo"),
                "aviso_cobertura": "El coste cargado cubre poco periodo: el margen a origen NO es representativo." if cob < 0.5 else None,
                "total": {"venta": m(df["certificado_m"].sum()), "coste": m(df["coste_m"].sum()),
                          "margen": m(df["margen_m"].sum())},
                "capitulos": [{"codigo": r.codigo, "capitulo": r.capitulo, "venta": m(r.certificado_m), "coste": m(r.coste_m),
                               "margen": m(r.margen_m), "margen_pct": r.margen_pct, "contratado": m(r.contratado_m),
                               "avance_pct": r.avance_pct} for r in df.itertuples() if r.certificado_m or r.coste_m],
                "alertas": [x[1] for x in oc.alertas_rentabilidad(df, cob if (a.get("modo") == "origen") else 1.0)]}

    if nombre == "auditoria_mensual":
        from . import auditoria as A, obra_control as oc
        o = _resolver_obra(con, a.get("obra"))
        c = oc.ultima_cert(con, o["id"]) if o else None
        if not c:
            return {"error": "Obra sin certificación"}
        r = A.calcular(con, o["id"], c["id"])
        pr = A.proyeccion(r)
        E = lambda v: fmt_eur(A.r2(v))  # noqa: E731
        return {"certificacion": c["numero"], "ventas": E(r["ventas"]), "compras": E(r["compras"]), "rrhh": E(r["rrhh"]),
                "resultado_sis": E(r["resultado_sis"]), "pct_sis": f"{A.r2(r['pct_sis'])} %", "correccion": E(r["correccion"]),
                "resultado": E(r["resultado"]), "pct": f"{A.r2(r['pct'])} %", "proyeccion": E(pr["proyeccion"]),
                "pct_proyeccion": f"{A.r2(pr['pct'])} %",
                "categorias": [{"categoria": x["categoria"], "cobrado": E(x["cobrado"]), "sin_pase": E(x["sin_pase"]),
                                "pagado": E(x["pagado"]), "diferencial": E(x["diferencial"]), "terminada": x["terminada"]}
                               for x in r["categorias"] if x["cobrado"] or x["pagado"]],
                "controles": {"venta_clasificada_pct": str(A.r2(r["pct_ventas_clasif"])),
                              "partidas_sin_criterio": r["partidas_sin_criterio"],
                              "proveedores_sin_categoria": len(r["proveedores_sin_categoria"])}}

    if nombre == "historico_precios":
        o = _resolver_obra(con, a.get("obra")) if a.get("obra") else None
        df = analytics.historico_precios(con, a["texto"], obra_id=o and o["id"])
        return {"n": len(df), "registros": df.head(50).to_dict("records")}

    return {"error": f"Herramienta desconocida: {nombre}"}


SYSTEM = """Eres el asistente financiero de obra de Llorca Group (constructora, Benidorm).
Respondes en español, de forma directa y profesional, a jefes de obra, administración y dirección.

REGLAS DE ORO (hay mucho dinero en juego):
1. Toda cifra que des debe salir de una herramienta. NUNCA calcules sumas, porcentajes o totales por tu cuenta;
   si necesitas un total, llama a la herramienta que lo devuelve. Si no tienes el dato, dilo.
2. Cita el nº de factura / proveedor / id de documento cuando hables de facturas concretas.
3. El coste de obra se mide por BASE IMPONIBLE (el IVA no es coste). Aclara si mezclas conceptos
   (base, total factura, líquido a pagar, retención de garantía).
4. Si una herramienta indica incidencias graves o datos pendientes de revisión, adviértelo: las cifras de facturas
   no aprobadas son provisionales.
5. No tomes decisiones contables ni de pago: informa y recomienda revisar. La decisión es humana.
6. Formato: respuestas breves; usa tablas markdown cuando compares varias partidas o proveedores.
Hoy es {hoy}."""


def responder(con, api_key: str, model: str, historial: list[dict], max_iter: int = 8):
    """Ejecuta el bucle agente. Devuelve (texto, traza_herramientas, historial_actualizado)."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, max_retries=3)
    msgs = list(historial)
    traza = []
    for _ in range(max_iter):
        r = client.messages.create(model=model, max_tokens=4000, temperature=0,
                                   system=SYSTEM.format(hoy=date.today().isoformat()),
                                   tools=TOOLS, messages=msgs)
        msgs.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in r.content]})
        if r.stop_reason != "tool_use":
            texto = "".join(b.text for b in r.content if b.type == "text")
            return texto, traza, msgs
        resultados = []
        for b in r.content:
            if b.type == "tool_use":
                try:
                    out = ejecutar_herramienta(con, b.name, b.input or {})
                except Exception as e:  # la herramienta nunca debe tumbar el chat
                    out = {"error": str(e)}
                traza.append({"herramienta": b.name, "parametros": b.input, "resultado": out})
                resultados.append({"type": "tool_result", "tool_use_id": b.id,
                                   "content": json.dumps(out, ensure_ascii=False, default=str)[:60000]})
        msgs.append({"role": "user", "content": resultados})
    return "He alcanzado el límite de consultas para esta pregunta. Reformúlala de forma más concreta.", traza, msgs



def responder_ollama(con, url: str, modelo: str, historial: list[dict], max_iter: int = 6, opciones: dict | None = None):
    """
    Asistente con modelo local. Primero intenta 'tool calling' nativo; si el modelo o Ollama fallan,
    usa un modo de respaldo en dos pasos (elegir consultas en JSON -> redactar con los datos). Nada sale del equipo.
    """
    from .ollama_cliente import chat, OllamaError, capacidades
    caps = capacidades(url, modelo)
    if caps and "tools" not in caps:
        # el modelo no admite herramientas (p. ej. algunos Vision): directamente al modo de consultas planificadas
        return _ollama_router(con, url, modelo, historial, opciones)
    try:
        return _ollama_tools(con, url, modelo, historial, max_iter, opciones, chat)
    except (OllamaError, _Vacio):
        return _ollama_router(con, url, modelo, historial, opciones)


def _ollama_tools(con, url, modelo, historial, max_iter, opciones, chat):
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["input_schema"]}} for t in TOOLS]
    msgs = [{"role": "system", "content": SYSTEM.format(hoy=date.today().isoformat())}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in historial[-8:] if isinstance(m.get("content"), str)]
    traza = []
    for _ in range(max_iter):
        msg = chat(url, modelo, msgs, tools=tools, opciones=opciones)
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []})
        calls = msg.get("tool_calls") or []
        if not calls:
            texto = msg.get("content") or ""
            if not texto.strip():
                raise _Vacio()
            return texto, traza, historial + [{"role": "assistant", "content": texto}]
        for c in calls:
            f = c.get("function", {})
            args = _args(f.get("arguments"))
            out = _ejecutar_seguro(con, f.get("name"), args)
            traza.append({"herramienta": f.get("name"), "parametros": args, "resultado": out})
            msgs.append({"role": "tool", "tool_name": f.get("name"),
                         "content": json.dumps(out, ensure_ascii=False, default=str)[:20000]})
    return "He alcanzado el límite de consultas para esta pregunta.", traza, historial


class _Vacio(Exception):
    pass


def _args(a):
    if isinstance(a, str):
        try:
            return json.loads(a)
        except Exception:
            return {}
    return a or {}


def _ejecutar_seguro(con, nombre, args):
    try:
        return ejecutar_herramienta(con, nombre, args)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _ollama_router(con, url, modelo, historial, opciones):
    """Respaldo: el modelo elige hasta 3 consultas en JSON; se ejecutan; después redacta SOLO con esos datos."""
    from .ollama_cliente import chat, parse_json
    pregunta = next((m["content"] for m in reversed(historial) if m["role"] == "user" and isinstance(m["content"], str)), "")
    catalogo = "\n".join(f"- {t['name']}: {t['description']} Parámetros: "
                          f"{json.dumps(t['input_schema'].get('properties', {}), ensure_ascii=False)}" for t in TOOLS)
    m = chat(url, modelo, [
        {"role": "system", "content": "Eres el planificador de consultas de un sistema de control de obra. "
                                      "Elige qué consultas hacer para responder. Responde SOLO JSON."},
        {"role": "user", "content": f"Consultas disponibles:\n{catalogo}\n\nObras: "
                                    f"{', '.join(o['codigo'] + ' ' + o['nombre'] for o in db.rows(con, 'SELECT codigo, nombre FROM obras'))}"
                                    f"\nHoy: {date.today().isoformat()}\n\nPregunta: {pregunta}\n\n"
                                    'Devuelve {"consultas": [{"herramienta": "nombre", "parametros": {...}}]} con 1 a 3 consultas, '
                                    'o {"consultas": []} si la pregunta no necesita datos.'}],
        formato="json", opciones=opciones)
    try:
        plan = parse_json(m.get("content") or "").get("consultas", [])[:3]
    except Exception:
        plan = []
    if not plan and re.search(r"(?i)cu[aá]nt|coste|gast|factur|proveedor|partida|margen|obra|pag|certific|oferta|iva|retenci", pregunta):
        plan = [{"herramienta": "resumen_obra", "parametros": {}}]
    traza = []
    for c in plan:
        nombre, args = c.get("herramienta"), _args(c.get("parametros"))
        out = _ejecutar_seguro(con, nombre, args)
        traza.append({"herramienta": nombre, "parametros": args, "resultado": out})
    datos = json.dumps([{"consulta": t["herramienta"], "resultado": t["resultado"]} for t in traza], ensure_ascii=False, default=str)
    m = chat(url, modelo, [
        {"role": "system", "content": SYSTEM.format(hoy=date.today().isoformat())},
        {"role": "user", "content": f"Pregunta: {pregunta}\n\nDATOS DEL SISTEMA (usa SOLO estas cifras, no calcules otras):\n{datos[:16000]}"}],
        opciones=opciones)
    texto = m.get("content") or "No he podido redactar la respuesta."
    return texto, traza, historial + [{"role": "assistant", "content": texto}]


# ============================================================================ historial persistente
def init_chat(con):
    con.executescript("""CREATE TABLE IF NOT EXISTS chat_mensajes (
        id INTEGER PRIMARY KEY, usuario TEXT NOT NULL, conv INTEGER NOT NULL DEFAULT 1,
        rol TEXT NOT NULL, contenido TEXT, traza TEXT, ts TEXT)""")
    con.commit()


def conv_actual(con, usuario: str) -> int:
    r = db.one(con, "SELECT MAX(conv) c FROM chat_mensajes WHERE usuario=?", (usuario,))
    return (r and r["c"]) or 1


def mensajes(con, usuario: str) -> list[dict]:
    c = conv_actual(con, usuario)
    out = []
    for r in db.rows(con, "SELECT rol, contenido, traza FROM chat_mensajes WHERE usuario=? AND conv=? ORDER BY id", (usuario, c)):
        out.append({"role": r["rol"], "texto": r["contenido"], "traza": json.loads(r["traza"]) if r["traza"] else []})
    return out


def guardar_mensaje(con, usuario: str, rol: str, texto: str, traza=None) -> None:
    con.execute("INSERT INTO chat_mensajes (usuario, conv, rol, contenido, traza, ts) VALUES (?,?,?,?,?,?)",
                (usuario, conv_actual(con, usuario), rol, texto, json.dumps(traza or [], ensure_ascii=False, default=str)[:200000],
                 db.now_iso()))
    con.commit()


def nueva_conversacion(con, usuario: str) -> None:
    con.execute("INSERT INTO chat_mensajes (usuario, conv, rol, contenido, ts) VALUES (?,?,?,?,?)",
                (usuario, conv_actual(con, usuario) + 1, "sistema", "", db.now_iso()))
    con.commit()


# ============================================================================ vía rápida (sin esperar al modelo)
INTENCIONES = [
    (r"retenci[oó]n|garant[ií]a|devolver", [("retenciones_garantia", {})]),
    (r"vencid|pendiente[s]? de pago|por pagar|pagos? pendiente|deb[eo]mos", [("pagos_pendientes", {})]),
    (r"duplicad|iban|incidencia|fraude|cuenta bancaria", [("incidencias_abiertas", {})]),
    (r"margen|rentab|beneficio|p[eé]rdida|ganando|perdiendo", [("rentabilidad_capitulos", {"modo": "mes"})]),
    (r"certificaci[oó]n|certificado|orden(es)? de cambio", [("certificacion_resumen", {})]),
    (r"oferta|contrat|adjudic|presupuesto de", [("ofertas_contratos", {})]),
    (r"proveedor(es)?|industrial(es)?|qui[eé]n (cobra|factura)|pareto|80 ?%", [("coste_por_proveedor", {})]),
    (r"partida|cap[ií]tulo|en qu[eé] (se|hemos|llevamos)", [("coste_por_partida", {})]),
    (r"mes a mes|evoluci[oó]n|mensual|por meses", [("evolucion_mensual", {})]),
    (r"cu[aá]nto (llevamos|hemos|se ha)|gastado|coste total|total de la obra|resumen", [("resumen_obra", {})]),
]


def plan_rapido(con, pregunta: str) -> list[tuple[str, dict]]:
    """Traduce la pregunta a consultas SIN modelo de lenguaje (instantáneo). Lista vacía si no está clara."""
    q = pregunta.lower()
    obra = None
    for o in db.rows(con, "SELECT codigo, nombre, alias FROM obras"):
        if re.search(rf"\b{re.escape(o['codigo'].lower())}\b", q) or o["nombre"].lower().split()[0] in q:
            obra = o["codigo"]
    if not obra:
        r = db.rows(con, "SELECT codigo FROM obras WHERE activa=1")
        obra = r[0]["codigo"] if len(r) == 1 else None
    plan = []
    m = re.search(r"(?:gastado|pagado|pagamos|comprado|coste|cuesta|gasto)\s+(?:en|de)\s+(?:el |la |los |las )?([a-záéíóúñ ]{3,40}?)(?:\s+y\b|\?|$|,)", q)
    if m and not re.search(r"obra|total", m.group(1)):
        plan.append(("buscar_lineas", {"texto": m.group(1).strip(), **({"obra": obra} if obra else {})}))
    for pat, consultas in INTENCIONES:
        if re.search(pat, q):
            for nombre, args in consultas:
                a = dict(args)
                if obra and nombre not in ("incidencias_abiertas",) or nombre in ("coste_por_partida", "rentabilidad_capitulos",
                                                                                    "certificacion_resumen", "ofertas_contratos"):
                    if obra:
                        a["obra"] = obra
                if nombre in ("coste_por_partida", "rentabilidad_capitulos", "certificacion_resumen", "ofertas_contratos") and "obra" not in a:
                    continue
                plan.append((nombre, a))
            if len(plan) >= 2:
                break
    vistos, out = set(), []
    for p in plan:
        k = (p[0], json.dumps(p[1], sort_keys=True))
        if k not in vistos:
            vistos.add(k)
            out.append(p)
    return out[:3]


def redactar_sin_ia(traza: list[dict]) -> str:
    """Respuesta inmediata con las cifras exactas (sin redacción por IA)."""
    partes = []
    for t in traza:
        r = t["resultado"]
        partes.append(f"**{t['herramienta'].replace('_', ' ').capitalize()}**")
        if isinstance(r, dict):
            simples = {k: v for k, v in r.items() if not isinstance(v, (list, dict)) and v not in (None, "")}
            for k, v in list(simples.items())[:10]:
                partes.append(f"- {k.replace('_', ' ')}: **{v}**")
            for k, v in r.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    cols = list(v[0].keys())[:6]
                    partes.append("\n| " + " | ".join(c.replace("_", " ") for c in cols) + " |\n|" + "---|" * len(cols))
                    for fila in v[:12]:
                        partes.append("| " + " | ".join(str(fila.get(c, ""))[:40] for c in cols) + " |")
                    if len(v) > 12:
                        partes.append(f"\n… y {len(v) - 12} más.")
                    break
                if isinstance(v, dict) and v:
                    for kk, vv in list(v.items())[:10]:
                        partes.append(f"- {kk}: {vv if not isinstance(vv, dict) else ', '.join(f'{a}: {b}' for a, b in vv.items())}")
                    break
        partes.append("")
    return "\n".join(partes) or "No he encontrado datos para esa pregunta."


def responder_rapido(con, pregunta: str, historial: list[dict], redactar=None) -> tuple[str, list, bool] | None:
    """Si la intención es clara: consultas deterministas al instante y, opcionalmente, redacción breve con el modelo."""
    plan = plan_rapido(con, pregunta)
    if not plan:
        return None
    traza = [{"herramienta": n, "parametros": a, "resultado": _ejecutar_seguro(con, n, a)} for n, a in plan]
    if redactar:
        try:
            return redactar(pregunta, traza), traza, True
        except Exception:
            pass
    return redactar_sin_ia(traza), traza, False
