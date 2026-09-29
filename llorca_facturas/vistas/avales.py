"""Avales y garantías: vencimientos, comisiones que siguen corriendo y devolución de los avales físicos."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import db
from core.money import parse_amount, to_cents, fmt_eur, from_cents
from vistas.comun import get_con, usuario, selector_obra, eur_col, puede_editar

SCHEMA = """CREATE TABLE IF NOT EXISTS avales (
    id INTEGER PRIMARY KEY, obra_id INTEGER REFERENCES obras(id) ON DELETE SET NULL,
    entidad TEXT, numero TEXT, tipo TEXT, beneficiario TEXT, importe_cents INTEGER, comision_pct TEXT,
    fecha_emision TEXT, fecha_vencimiento TEXT, formato TEXT, estado TEXT DEFAULT 'vivo', notas TEXT,
    creado_por TEXT, creado_en TEXT)"""
TIPOS = ["Fiel cumplimiento", "Garantía de anticipo", "Buena ejecución", "Licitación / provisional", "Otro"]
ESTADOS = ["vivo", "solicitada devolución", "devuelto", "cancelado"]


def render():
    con = get_con()
    con.executescript(SCHEMA)
    st.title("Avales y garantías")
    st.caption("Cada aval vivo cuesta comisión mientras no se cancela. Los electrónicos caducan solos; los físicos hay que recogerlos.")
    obra_id = selector_obra("aval_obra")
    q = "SELECT a.*, o.codigo AS obra FROM avales a LEFT JOIN obras o ON o.id=a.obra_id"
    df = pd.DataFrame(db.rows(con, q + (" WHERE a.obra_id=?" if obra_id else ""), (obra_id,) if obra_id else ()))
    hoy = date.today().isoformat()
    if not df.empty:
        vivos = df[df["estado"] == "vivo"]
        coste_anual = sum(from_cents(r.importe_cents) * parse_amount(r.comision_pct or "0") / 100 for r in vivos.itertuples())
        k = st.columns(4)
        k[0].metric("Avales vivos", len(vivos))
        k[1].metric("Importe avalado", fmt_eur(from_cents(int(vivos["importe_cents"].sum()))))
        k[2].metric("Comisión anual estimada", fmt_eur(coste_anual))
        venc = vivos[vivos["fecha_vencimiento"].fillna("9999") < hoy]
        k[3].metric("Vencidos sin cancelar", len(venc))
        for r in venc.itertuples():
            st.warning(f"Aval {r.numero or r.id} ({r.entidad}) venció el {r.fecha_vencimiento} y sigue vivo"
                       + (": es físico, hay que recuperarlo del beneficiario." if (r.formato or "") == "físico" else "."))
        vis = df.assign(importe=df["importe_cents"] / 100)
        ed = st.data_editor(vis[["id", "obra", "entidad", "numero", "tipo", "beneficiario", "importe", "comision_pct", "fecha_emision",
                                 "fecha_vencimiento", "formato", "estado", "notas"]], hide_index=True, width="stretch",
                            disabled=["id", "obra", "importe"], key="avales_ed",
                            column_config={"importe": eur_col("Importe (€)"), "comision_pct": "Comisión %",
                                           "estado": st.column_config.SelectboxColumn("Estado", options=ESTADOS),
                                           "formato": st.column_config.SelectboxColumn("Formato", options=["electrónico", "físico"])})
        if puede_editar() and st.button("Guardar cambios de avales"):
            with db.tx(con):
                for r in ed.itertuples():
                    con.execute("UPDATE avales SET entidad=?, numero=?, tipo=?, beneficiario=?, comision_pct=?, fecha_emision=?, "
                                "fecha_vencimiento=?, formato=?, estado=?, notas=? WHERE id=?",
                                (r.entidad, r.numero, r.tipo, r.beneficiario, r.comision_pct, r.fecha_emision, r.fecha_vencimiento,
                                 r.formato, r.estado, r.notas, int(r.id)))
                db.audit(con, usuario(), "editar_avales", "aval", None, None)
            st.rerun()
    if not puede_editar():
        return
    with st.expander("Registrar un aval"):
        with st.form("nuevo_aval", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            entidad = c1.text_input("Entidad (banco/aseguradora)")
            numero = c2.text_input("Nº de aval")
            tipo = c3.selectbox("Tipo", TIPOS)
            c4, c5, c6 = st.columns(3)
            benef = c4.text_input("Beneficiario")
            imp = c5.text_input("Importe (€)")
            com = c6.text_input("Comisión anual %", "0,5")
            c7, c8, c9 = st.columns(3)
            fe = c7.date_input("Emisión", format="DD/MM/YYYY")
            fv = c8.date_input("Vencimiento", value=None, format="DD/MM/YYYY")
            formato = c9.selectbox("Formato", ["electrónico", "físico"])
            if st.form_submit_button("Guardar aval", type="primary"):
                if not imp.strip() or not obra_id:
                    st.error("Elija la obra arriba e indique el importe.")
                else:
                    with db.tx(con):
                        cur = con.execute("INSERT INTO avales (obra_id, entidad, numero, tipo, beneficiario, importe_cents, comision_pct, "
                                          "fecha_emision, fecha_vencimiento, formato, creado_por, creado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                          (obra_id, entidad, numero, tipo, benef, to_cents(parse_amount(imp)), str(parse_amount(com)),
                                           fe.isoformat(), fv.isoformat() if fv else None, formato, usuario(), db.now_iso()))
                        db.audit(con, usuario(), "alta_aval", "aval", cur.lastrowid, {"importe": imp})
                    st.rerun()
