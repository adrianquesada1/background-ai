"""Diario de obra: reuniones, compromisos, decisiones y extras. La memoria de cada obra, con alertas."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import db, gobierno as G, maestros
from core.money import parse_amount, to_cents
from vistas.comun import get_con, usuario, selector_obra, rol


def render():
    con = get_con()
    G.init(con)
    st.title("Diario de obra")
    st.caption("Cada reunión se convierte en compromisos (qué, quién, cuándo), decisiones y posibles extras. Lo abierto pasa a la "
               "siguiente reunión y lo vencido avisa. Todo se revisa antes de guardarse.")
    obra_id = selector_obra("gob_obra", permitir_todas=False)
    if not obra_id:
        return
    editar = rol() in ("admin", "direccion", "gestor", "jefe_obra", "tecnico")
    venc = G.vencidos(con, obra_id)
    ab = G.abiertos(con, obra_id)
    k = st.columns(4)
    k[0].metric("Compromisos abiertos", sum(1 for x in ab if x["tipo"] != "extra"))
    k[1].metric("Vencidos", len(venc))
    k[2].metric("Extras por valorar", sum(1 for x in ab if x["tipo"] == "extra"))
    k[3].metric("Reuniones registradas", db.one(con, "SELECT COUNT(*) n FROM obra_reuniones WHERE obra_id=?", (obra_id,))["n"])
    for v in venc[:5]:
        st.warning(f"Vencido el {v['fecha_limite']}: {v['descripcion'][:120]}" + (f" (responsable: {v['responsable']})" if v["responsable"] else ""))
    tabs = st.tabs(["Nueva reunión", "Asuntos abiertos", "Extras y órdenes de cambio", "Reuniones y actas"])

    with tabs[0]:
        if not editar:
            st.caption("Su rol no puede registrar reuniones.")
        else:
            c1, c2 = st.columns([1, 3])
            fecha = c1.date_input("Fecha", date.today(), format="DD/MM/YYYY", key="gob_fecha")
            titulo = c2.text_input("Asunto", placeholder="Reunión semanal de obra nº 32", key="gob_tit")
            asist = st.text_input("Asistentes", placeholder="Constructora, Project Manager, Dirección Facultativa…", key="gob_asist")
            if ab:
                with st.expander(f"Asuntos abiertos de reuniones anteriores ({len(ab)}) — repáselos en esta reunión"):
                    for x in ab:
                        st.markdown(f"- {G.TIPOS.get(x['tipo'], x['tipo'])}: {x['descripcion'][:140]}"
                                    + (f" · {x['responsable']}" if x["responsable"] else "") + (f" · plazo {x['fecha_limite']}" if x["fecha_limite"] else ""))
            notas = st.text_area("Notas de la reunión", height=220, key="gob_notas",
                                 placeholder="Pegue o escriba las notas. Ej.: «Juan enviará los planos de fachada antes del 15/10.»\n"
                                             "«La Propiedad solicita cambiar el pavimento de terrazas: extra a valorar (aprox. 3.500 €).»\n"
                                             "«Se acuerda adelantar la entrega del bloque B.»")
            if st.button("Analizar notas", icon=":material/auto_awesome:", disabled=not notas.strip()):
                st.session_state["gob_prop"] = G.analizar_notas(notas, fecha)
            prop = st.session_state.get("gob_prop")
            if prop is not None:
                partidas = maestros.partidas_de_obra(con, obra_id)
                labp = {p["id"]: f"{p['codigo']} · {p['descripcion'][:35]}" for p in partidas}
                for x in prop:
                    x["capitulo"] = None
                    if x["tipo"] == "extra":
                        pid, _ = maestros.clasificar_linea(x["descripcion"], partidas)
                        x["capitulo"] = labp.get(pid)
                df = pd.DataFrame(prop, columns=["tipo", "descripcion", "responsable", "fecha_limite", "importe", "capitulo"])
                st.caption(f"{len(df)} elemento(s) detectados. Corrija, añada o borre filas antes de guardar.")
                ed = st.data_editor(df, num_rows="dynamic", width="stretch", key="gob_ed", column_config={
                    "tipo": st.column_config.SelectboxColumn("Tipo", options=list(G.TIPOS)),
                    "descripcion": st.column_config.TextColumn("Descripción", width="large"), "responsable": "Responsable",
                    "fecha_limite": st.column_config.TextColumn("Plazo (AAAA-MM-DD)"), "importe": "Importe estimado (€)",
                    "capitulo": st.column_config.SelectboxColumn("Capítulo (extras)", options=list(labp.values()))})
                if st.button("Guardar reunión", type="primary"):
                    inv = {v: k_ for k_, v in labp.items()}
                    with db.tx(con):
                        cur = con.execute("INSERT INTO obra_reuniones (obra_id, fecha, titulo, asistentes, notas, creado_por, creado_en) "
                                          "VALUES (?,?,?,?,?,?,?)", (obra_id, fecha.isoformat(), titulo, asist, notas, usuario(), db.now_iso()))
                        rid = cur.lastrowid
                        n = 0
                        for r in ed.fillna("").to_dict("records"):
                            if not str(r["descripcion"]).strip():
                                continue
                            con.execute("""INSERT INTO obra_compromisos (obra_id, reunion_id, tipo, descripcion, responsable, fecha_limite,
                                           partida_id, importe_estimado_cents, creado_por, creado_en) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                        (obra_id, rid, r["tipo"] or "compromiso", r["descripcion"], r["responsable"] or None,
                                         r["fecha_limite"] or None, inv.get(r["capitulo"]),
                                         to_cents(parse_amount(str(r["importe"]))) if str(r["importe"]).strip() else None, usuario(), db.now_iso()))
                            n += 1
                        db.audit(con, usuario(), "reunion_obra", "obra", obra_id, {"reunion": rid, "elementos": n})
                    st.session_state.pop("gob_prop", None)
                    st.success(f"Reunión guardada con {n} elemento(s)."); st.rerun()

    with tabs[1]:
        items = pd.DataFrame(db.rows(con, "SELECT id, tipo, descripcion, responsable, fecha_limite, estado FROM obra_compromisos "
                                          "WHERE obra_id=? AND tipo<>'extra' ORDER BY estado, COALESCE(fecha_limite,'9999')", (obra_id,)))
        if items.empty:
            st.caption("Sin compromisos registrados.")
        else:
            items["vencido"] = items.apply(lambda r: r["estado"] == "abierto" and bool(r["fecha_limite"]) and r["fecha_limite"] < date.today().isoformat(), axis=1)
            ed = st.data_editor(items, hide_index=True, width="stretch", key="gob_items", disabled=["id", "vencido"] if editar else True,
                                column_config={"tipo": st.column_config.SelectboxColumn("Tipo", options=list(G.TIPOS)),
                                               "estado": st.column_config.SelectboxColumn("Estado", options=["abierto", "cumplido", "cancelado"]),
                                               "descripcion": st.column_config.TextColumn("Descripción", width="large"),
                                               "vencido": st.column_config.CheckboxColumn("Vencido")})
            if editar and st.button("Guardar cambios de compromisos"):
                with db.tx(con):
                    for r in ed.to_dict("records"):
                        con.execute("""UPDATE obra_compromisos SET tipo=?, descripcion=?, responsable=?, fecha_limite=?, estado=?,
                                       cerrado_por=CASE WHEN ?<>'abierto' THEN ? ELSE NULL END,
                                       cerrado_en=CASE WHEN ?<>'abierto' THEN ? ELSE NULL END WHERE id=?""",
                                    (r["tipo"], r["descripcion"], r["responsable"], r["fecha_limite"], r["estado"], r["estado"], usuario(),
                                     r["estado"], db.now_iso(), int(r["id"])))
                    db.audit(con, usuario(), "editar_compromisos", "obra", obra_id, None)
                st.rerun()

    with tabs[2]:
        ex = pd.DataFrame(db.rows(con, """SELECT c.id, c.descripcion, c.responsable, p.codigo AS capitulo, c.importe_estimado_cents/100.0 AS importe,
                                          c.estado, c.creado_en FROM obra_compromisos c LEFT JOIN partidas p ON p.id=c.partida_id
                                          WHERE c.obra_id=? AND c.tipo='extra' ORDER BY c.id DESC""", (obra_id,)))
        if ex.empty:
            st.caption("Sin extras detectados. Se registran desde las notas de reunión.")
        else:
            st.caption("Lo que no se escribe no se cobra: cada extra se ancla a un capítulo y tiene su borrador de orden de cambio.")
            ed = st.data_editor(ex, hide_index=True, width="stretch", key="gob_ex", disabled=["id", "capitulo", "creado_en"] if editar else True,
                                column_config={"estado": st.column_config.SelectboxColumn("Estado", options=["abierto", "valorado", "aprobado", "cancelado"]),
                                               "importe": st.column_config.NumberColumn("Importe (€)", format="localized"),
                                               "descripcion": st.column_config.TextColumn("Descripción", width="large")})
            c1, c2 = st.columns([3, 1])
            if editar and c1.button("Guardar cambios de extras"):
                with db.tx(con):
                    for r in ed.to_dict("records"):
                        con.execute("UPDATE obra_compromisos SET descripcion=?, responsable=?, importe_estimado_cents=?, estado=? WHERE id=?",
                                    (r["descripcion"], r["responsable"], int(round(float(r["importe"]) * 100)) if r["importe"] == r["importe"] and r["importe"] is not None else None,
                                     r["estado"], int(r["id"])))
                    db.audit(con, usuario(), "editar_extras", "obra", obra_id, None)
                st.rerun()
            sel = st.selectbox("Borrador de orden de cambio para", ex["id"].tolist(), key="gob_oc",
                               format_func=lambda i: ex.set_index("id").loc[i, "descripcion"][:90])
            st.download_button("Descargar borrador de orden de cambio", G.orden_cambio_html(con, sel), file_name=f"orden_cambio_{sel}.html",
                               mime="text/html")

    with tabs[3]:
        reus = db.rows(con, "SELECT id, fecha, titulo, asistentes FROM obra_reuniones WHERE obra_id=? ORDER BY fecha DESC", (obra_id,))
        if not reus:
            st.caption("Sin reuniones.")
        for r in reus:
            with st.container(border=True):
                a, b, c = st.columns([4, 1, 1])
                a.markdown(f"**{r['fecha']}** · {r['titulo'] or 'Reunión'}  \n:gray[{r['asistentes'] or ''}]")
                b.download_button("Acta interna", G.acta_html(con, r["id"], "interna"), file_name=f"acta_interna_{r['fecha']}.html",
                                  mime="text/html", key=f"ai_{r['id']}")
                c.download_button("Acta DF", G.acta_html(con, r["id"], "df"), file_name=f"acta_DF_{r['fecha']}.html",
                                  mime="text/html", key=f"adf_{r['id']}")
