"""Maestro de proveedores e historial de cuentas bancarias (control antifraude)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import db, analytics
from core.fiscal import validate_nif, validate_iban, format_iban
from core.validation import validar_y_guardar
from vistas.comun import get_con, usuario, eur_col, cents_to_eur


def render():
    con = get_con()
    st.title("Proveedores")
    prov = pd.DataFrame(db.rows(con, """
        SELECT p.id, p.nombre, p.nif, p.tipo,
               COUNT(d.id) AS docs, COALESCE(SUM(d.base_imponible_cents),0) AS base_c,
               COALESCE(SUM(d.ret_garantia_cents),0) AS ret_c,
               (SELECT COUNT(*) FROM proveedor_ibans i WHERE i.proveedor_id=p.id) AS n_ibans
        FROM proveedores p LEFT JOIN documentos d ON d.proveedor_id=p.id AND d.estado<>'rechazada'
        GROUP BY p.id ORDER BY base_c DESC"""))
    if prov.empty:
        st.info("Los proveedores se crean solos al leer facturas.")
        return
    prov["nif_ok"] = prov["nif"].apply(lambda n: validate_nif(n)[0] if n else False)
    st.dataframe(cents_to_eur(prov, {"base_c": "Base", "ret_c": "Ret. garantía"}), hide_index=True, width="stretch",
                 column_config={"Base": eur_col("Base facturada (€)"), "Ret. garantía": eur_col("Ret. garantía (€)"),
                                "nif_ok": st.column_config.CheckboxColumn("NIF válido"), "n_ibans": "Nº IBAN",
                                "docs": "Docs"})
    with st.expander("Correos de contacto (avisos de pago, autorizaciones de facturación, CAE)"):
        st.caption("Si no se indica, se usa el remitente habitual de sus facturas en el buzón.")
        mails = pd.DataFrame(db.rows(con, "SELECT id, nombre, email, email_administracion FROM proveedores ORDER BY nombre"))
        ed_m = st.data_editor(mails, hide_index=True, width="stretch", key="prov_mails", disabled=["id", "nombre"],
                              column_config={"id": None, "nombre": "Proveedor", "email": "Correo general", "email_administracion": "Correo de administración"})
        if st.button("Guardar correos"):
            from core.correo import validar_direcciones
            malos = [r["nombre"] for r in ed_m.fillna("").to_dict("records") if validar_direcciones(r["email"]) or validar_direcciones(r["email_administracion"])]
            if malos:
                st.error("Correo no válido en: " + ", ".join(malos))
            else:
                with db.tx(con):
                    for r in ed_m.fillna("").to_dict("records"):
                        con.execute("UPDATE proveedores SET email=?, email_administracion=? WHERE id=?",
                                    (r["email"].strip() or None, r["email_administracion"].strip() or None, r["id"]))
                    db.audit(con, usuario(), "correos_proveedores", "proveedor", None, None)
                st.success("Guardado.")
    multi = prov[prov["n_ibans"] > 1]
    if not multi.empty:
        st.warning(f" {len(multi)} proveedor(es) han usado más de una cuenta bancaria: "
                   + ", ".join(multi["nombre"]) + ". Verifica cada cuenta nueva por teléfono antes de pagar.")

    # ------------------------------------------------------------------ duplicados probables y fusión
    from rapidfuzz import fuzz
    from core import maestros as _m
    lista = prov.to_dict("records")
    sospechas = []
    for i, a in enumerate(lista):
        for b in lista[i + 1:]:
            mismo_nif = a["nif"] and a["nif"] == b["nif"]
            parecido = fuzz.token_set_ratio(_m.strip_accents(a["nombre"]), _m.strip_accents(b["nombre"])) >= 88
            if (parecido and (not a["nif"] or not b["nif"] or a["nif"] != b["nif"])) or mismo_nif:
                sospechas.append((a, b))
    with st.expander(f"Fusionar proveedores duplicados ({len(sospechas)} posibles)", expanded=bool(sospechas)):
        st.caption("Une dos fichas del mismo proveedor (p. ej. una creada sin NIF). Sus facturas, ofertas y cuentas pasan a la "
                   "ficha que se conserva. Se puede deshacer en «Papelera y deshacer».")
        for a, b in sospechas[:10]:
            st.markdown(f"- **{a['nombre']}** ({a['nif'] or 'sin NIF'}, {a['docs']} docs) ↔ **{b['nombre']}** "
                        f"({b['nif'] or 'sin NIF'}, {b['docs']} docs)")
        ids_ = prov["id"].tolist()
        f1, f2, f3 = st.columns([2, 2, 1])
        dest = f1.selectbox("Conservar", ids_, key="fus_dest", format_func=lambda i: f"{prov.set_index('id').loc[i, 'nombre']} "
                                                                                    f"({prov.set_index('id').loc[i, 'nif'] or 's/NIF'})")
        orig = f2.selectbox("Absorber (desaparece)", [i for i in ids_ if i != dest], key="fus_orig",
                            format_func=lambda i: f"{prov.set_index('id').loc[i, 'nombre']} ({prov.set_index('id').loc[i, 'nif'] or 's/NIF'})")
        a_nif, b_nif = prov.set_index("id").loc[dest, "nif"], prov.set_index("id").loc[orig, "nif"] if orig else None
        if a_nif and b_nif and a_nif != b_nif:
            st.warning("Los dos tienen NIF distinto: probablemente NO son el mismo proveedor.")
        if f3.button("Fusionar", disabled=orig is None, type="primary"):
            from core import historial as H
            H.init(con)
            H.fusionar_proveedores(con, dest, orig, usuario())
            st.toast("Proveedores fusionados"); st.rerun()

    st.subheader("Ficha")
    pid = st.selectbox("Proveedor", prov["id"].tolist(),
                       format_func=lambda i: f"{prov.set_index('id').loc[i, 'nombre']} ({prov.set_index('id').loc[i, 'nif'] or 's/NIF'})")
    p = db.one(con, "SELECT * FROM proveedores WHERE id=?", (pid,))
    with st.form(f"prov_{pid}"):
        c1, c2 = st.columns([3, 1])
        nombre = c1.text_input("Nombre", p["nombre"])
        tipo = c2.selectbox("Tipo", ["", "subcontrata", "suministro", "servicio", "profesional", "otro"],
                            index=["", "subcontrata", "suministro", "servicio", "profesional", "otro"].index(p["tipo"] or ""))
        notas = st.text_area("Notas", p["notas"] or "", height=68)
        if st.form_submit_button("Guardar"):
            with db.tx(con):
                con.execute("UPDATE proveedores SET nombre=?, tipo=?, notas=? WHERE id=?", (nombre, tipo, notas, pid))
                db.audit(con, usuario(), "editar_proveedor", "proveedor", pid, {"nombre": nombre, "tipo": tipo})
            st.rerun()

    st.markdown("**Cuentas bancarias usadas en sus facturas**")
    ib = db.rows(con, "SELECT * FROM proveedor_ibans WHERE proveedor_id=? ORDER BY primera_vez", (pid,))
    if not ib:
        st.caption("Sin IBAN registrado.")
    for i in ib:
        ok, msg = validate_iban(i["iban"])
        c1, c2, c3 = st.columns([3, 3, 2])
        c1.markdown(f"`{format_iban(i['iban'])}` {'' if ok else ' ' + msg}")
        c2.caption(f"Primera vez {i['primera_vez'] or '—'} · última {i['ultima_vez'] or '—'} · {i['veces']} factura(s)")
        if i["verificado"]:
            c3.success(f"Verificada por {i['verificado_por']}")
        elif c3.button("Marcar verificada", key=f"ver_{i['id']}",
                       help="Solo tras confirmar la cuenta con el proveedor por un canal independiente (teléfono conocido)."):
            with db.tx(con):
                con.execute("UPDATE proveedor_ibans SET verificado=1, verificado_por=? WHERE id=?", (usuario(), i["id"]))
                db.audit(con, usuario(), "verificar_iban", "proveedor", pid, {"iban": i["iban"]})
            for d in db.rows(con, "SELECT id FROM documentos WHERE proveedor_id=? AND iban=?", (pid, i["iban"])):
                validar_y_guardar(con, d["id"])
            st.rerun()

    st.markdown("**Documentos**")
    docs = analytics.documentos_df(con, proveedor_id=pid, estados=["pendiente_revision", "revisada", "aprobada", "rechazada"])
    if not docs.empty:
        st.dataframe(cents_to_eur(docs[["id", "numero", "fecha", "tipo_documento", "estado", "obra_codigo", "base_c", "ret_c", "pagar_c",
                                        "pagada"]], {"base_c": "Base", "ret_c": "Ret.", "pagar_c": "A pagar"}),
                     hide_index=True, width="stretch",
                     column_config={"Base": eur_col("Base (€)"), "Ret.": eur_col("Ret. (€)"), "A pagar": eur_col("A pagar (€)")})
