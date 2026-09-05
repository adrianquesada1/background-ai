"""
app.py — Croma IA en Streamlit
--------------------------------
Recorta el fondo de tus fotos con un modelo de IA real (rembg / U2-Net,
gratis, corre en local) y lo sustituye por un fondo limpio tipo foto de
producto (blanco, gris estudio, madera, o el que subas). Funciona con
cualquier fondo original, no solo verde.

Ejecutar con:
    streamlit run app.py

La primera vez que proceses una foto, se descarga el modelo de IA
(~176 MB) una sola vez; luego queda cacheado en tu equipo.
"""

import base64
import io
import zipfile
from pathlib import Path

import streamlit as st
from PIL import Image

from croma_ia_core import ParametrosIA, procesar_imagen_ia, obtener_session, crear_fondo_estudio

st.set_page_config(page_title="Croma IA — fotos de producto", page_icon="✨", layout="wide")

FONDO_MADERA = Path(__file__).parent / "fondo_madera_default.jpg"

# ---------------------------------------------------------------------------
# Estilos
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    .stApp { background: radial-gradient(ellipse at top, #1a1a1a 0%, #141414 60%); }

    .cabecera {
        display: flex; align-items: center; gap: 16px;
        padding: 4px 0 20px;
        border-bottom: 1px solid #2a2a2a;
        margin-bottom: 24px;
    }
    .cabecera .icono {
        width: 46px; height: 46px; border-radius: 12px;
        background: linear-gradient(145deg, #D4A857, #B98A3D);
        display: flex; align-items: center; justify-content: center;
        font-size: 1.4rem; flex-shrink: 0;
    }
    .cabecera h1 {
        font-size: 1.5rem; margin: 0; letter-spacing: -0.01em; color: #F2F0EC;
    }
    .cabecera p {
        margin: 2px 0 0; color: #9C9691; font-size: 0.88rem;
    }

    .paso-badge {
        display: inline-flex; align-items: center; justify-content: center;
        width: 22px; height: 22px; border-radius: 50%;
        background: #D4A857; color: #141414; font-size: 0.72rem; font-weight: 700;
        margin-right: 8px;
    }
    .paso-titulo {
        font-size: 1.05rem; font-weight: 600; color: #F2F0EC;
        display: flex; align-items: center; margin-bottom: 2px;
    }
    .paso-sub { color: #9C9691; font-size: 0.82rem; margin: 0 0 14px 30px; }

    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 14px !important;
    }

    /* Tarjetas de miniaturas de resultado: usan el contenedor nativo de
       Streamlit (más fiable que un div manual, que no envuelve bien los
       elementos generados por st.image/st.caption). */
    div[data-testid="column"] div[data-testid="stVerticalBlockBorderWrapper"] {
        transition: border-color 0.15s ease;
    }
    div[data-testid="column"] div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: #D4A857 !important;
    }

    .stButton > button[kind="primary"] {
        background: #D4A857; color: #141414; font-weight: 600; border: none;
    }
    .stButton > button[kind="primary"]:hover { background: #E6BC6E; }

    section[data-testid="stSidebar"] {
        border-right: 1px solid #2a2a2a;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="cabecera">
        <div class="icono">✨</div>
        <div>
            <h1>Croma IA</h1>
            <p>Recorte por IA + fondo de producto profesional, en segundos.</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource(show_spinner="Cargando el modelo de IA (solo la primera vez)...")
def cargar_modelo():
    return obtener_session("u2net")


# ---------------------------------------------------------------------------
# Barra lateral
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("#### 🖼️ Fondo final")
    tipo_fondo = st.radio(
        "Elige un fondo", ["Blanco liso", "Gris claro (estudio)", "Madera", "Subir el mío"],
        label_visibility="collapsed",
    )
    profundidad = st.toggle(
        "Efecto papel de estudio", value=True,
        help="Degradado sutil (más claro en el centro) como el papel continuo de un fotógrafo. "
        "Sin esto, un color plano se ve más '2D'.",
        disabled=tipo_fondo in ("Madera", "Subir el mío"),
    )

    TAMANO_FONDO = (1600, 1600)
    if tipo_fondo == "Subir el mío":
        fondo_file = st.file_uploader("Tu imagen de fondo", type=["jpg", "jpeg", "png"])
        fondo_img = Image.open(fondo_file).convert("RGB") if fondo_file else Image.new("RGB", TAMANO_FONDO, (255, 255, 255))
    elif tipo_fondo == "Madera":
        fondo_img = Image.open(FONDO_MADERA).convert("RGB")
    elif tipo_fondo == "Gris claro (estudio)":
        color = (232, 232, 230)
        fondo_img = crear_fondo_estudio(color, TAMANO_FONDO) if profundidad else Image.new("RGB", TAMANO_FONDO, color)
    else:
        color = (255, 255, 255)
        fondo_img = crear_fondo_estudio(color, TAMANO_FONDO) if profundidad else Image.new("RGB", TAMANO_FONDO, color)

    st.image(fondo_img, use_container_width=True)

    st.divider()
    st.markdown("#### 📐 Tamaño de lienzo")
    lienzo_fijo_activo = st.toggle(
        "Mismo tamaño para toda la tanda", value=False,
        help="Todas las fotos salen con el mismo tamaño y encuadre exactos, "
        "como en una tienda profesional. La prenda se escala para caber "
        "(sin deformar ni recortar), nunca al revés.",
    )
    PRESETS_TAMANO = {
        "1200 × 1500 (vertical 4:5, recomendado)": (1200, 1500),
        "1000 × 1000 (cuadrado 1:1)": (1000, 1000),
        "1080 × 1350 (formato Instagram)": (1080, 1350),
    }
    preset_elegido = st.selectbox(
        "Tamaño", list(PRESETS_TAMANO.keys()), disabled=not lienzo_fijo_activo,
    )
    tamano_fijo = PRESETS_TAMANO[preset_elegido] if lienzo_fijo_activo else None

    st.divider()
    st.markdown("#### ⚙️ Presentación")
    sombra = st.toggle("Sombra de suelo", value=True)
    contorno = st.toggle("Sombra de contorno (look 'elevado')", value=True)
    rotar = st.toggle("Auto-rotar a vertical", value=False)
    mejorar_color = st.toggle(
        "Realce de color (contraste + nitidez sutil)", value=True,
        help="Un pelín de contraste, saturación y nitidez, como el post-proceso "
        "que aplicaría un fotógrafo de producto. Sutil, no cambia los colores reales.",
    )

    with st.expander("Ajustes avanzados"):
        recortar = st.checkbox("Recortar ajustado a la prenda", value=True)
        margen = st.slider(
            "Margen de aire alrededor", 0.0, 0.40, 0.32, 0.02,
            help="Espacio de fondo alrededor de la prenda, para que no quede pegada a los bordes.",
            disabled=not recortar,
        )
        rotacion_manual = st.select_slider(
            "Rotación manual adicional", options=[0, 90, 180, 270], value=0,
            help="Si el giro automático no queda como quieres, ajusta aquí a mano.",
        )
        intensidad_sombra = st.slider("Intensidad sombra de suelo", 0.0, 0.5, 0.22, 0.02, disabled=not sombra)
        intensidad_contorno = st.slider("Intensidad sombra de contorno", 0.0, 0.7, 0.35, 0.05, disabled=not contorno)
        st.caption("Realce de color")
        col_c1, col_c2, col_c3 = st.columns(3)
        intensidad_contraste = col_c1.slider("Contraste", 1.0, 1.3, 1.06, 0.02, disabled=not mejorar_color, label_visibility="visible")
        intensidad_saturacion = col_c2.slider("Saturación", 1.0, 1.3, 1.08, 0.02, disabled=not mejorar_color, label_visibility="visible")
        intensidad_nitidez = col_c3.slider("Nitidez", 1.0, 1.5, 1.15, 0.05, disabled=not mejorar_color, label_visibility="visible")

params = ParametrosIA(
    rotar_a_vertical=rotar, rotacion_manual=rotacion_manual,
    recortar_ajustado=recortar, margen_recorte_pct=margen,
    añadir_sombra=sombra, intensidad_sombra=intensidad_sombra,
    sombra_contorno=contorno, intensidad_contorno=intensidad_contorno,
    tamano_fijo=tamano_fijo,
    mejorar_color=mejorar_color, intensidad_contraste=intensidad_contraste,
    intensidad_saturacion=intensidad_saturacion, intensidad_nitidez=intensidad_nitidez,
)

# ---------------------------------------------------------------------------
# Paso 1: subida
# ---------------------------------------------------------------------------
with st.container(border=True):
    st.markdown(
        '<div class="paso-titulo"><span class="paso-badge">1</span>Sube tus fotos</div>'
        '<p class="paso-sub">Cualquier fondo original vale, no hace falta que sea verde. Hasta 30 de golpe.</p>',
        unsafe_allow_html=True,
    )
    archivos = st.file_uploader(
        "Sube tus fotos", type=["jpg", "jpeg", "png"], accept_multiple_files=True,
        label_visibility="collapsed",
    )
    if archivos and len(archivos) > 30:
        st.warning(f"Has subido {len(archivos)} fotos, se procesarán solo las primeras 30.")
        archivos = archivos[:30]

    procesar = st.button("✨ Procesar fotos", type="primary", disabled=not archivos, use_container_width=True)

if "resultados_ia" not in st.session_state:
    st.session_state.resultados_ia = []

if procesar and archivos:
    session = cargar_modelo()
    st.session_state.resultados_ia = []
    barra = st.progress(0.0, text="Empezando...")
    for i, archivo in enumerate(archivos):
        try:
            im = Image.open(archivo)
            resultado = procesar_imagen_ia(im, session, fondo_img, params)
            st.session_state.resultados_ia.append((archivo.name, resultado, None))
        except Exception as e:
            st.session_state.resultados_ia.append((archivo.name, None, str(e)))
        barra.progress((i + 1) / len(archivos), text=f"Procesando {i+1}/{len(archivos)}")
    barra.empty()
    ok = sum(1 for _, im, err in st.session_state.resultados_ia if err is None)
    st.toast(f"Listo: {ok}/{len(archivos)} procesadas", icon="✅")

# ---------------------------------------------------------------------------
# Paso 2: resultados
# ---------------------------------------------------------------------------
resultados = st.session_state.resultados_ia

if resultados:
    st.write("")
    with st.container(border=True):
        ok_count = sum(1 for _, im, err in resultados if err is None)
        st.markdown(
            f'<div class="paso-titulo"><span class="paso-badge">2</span>Resultados '
            f'<span style="color:#9C9691; font-weight:400; margin-left:8px;">({ok_count}/{len(resultados)} correctas)</span></div>',
            unsafe_allow_html=True,
        )

        cols = st.columns(4)
        imagenes_ok = []
        for i, (nombre, im, err) in enumerate(resultados):
            with cols[i % 4]:
                if err:
                    st.error(f"❌ {nombre}\n\n{err}")
                else:
                    with st.container(border=True):
                        st.image(im, use_container_width=True)
                        st.caption(nombre)
                    imagenes_ok.append((nombre, im))
                st.write("")

    if imagenes_ok:
        st.write("")
        with st.container(border=True):
            st.markdown(
                '<div class="paso-titulo"><span class="paso-badge">3</span>Descarga</div>',
                unsafe_allow_html=True,
            )
            col_a, col_b = st.columns(2)

            with col_a:
                buffer_zip = io.BytesIO()
                with zipfile.ZipFile(buffer_zip, "w") as zf:
                    for nombre, im in imagenes_ok:
                        buf = io.BytesIO()
                        im.save(buf, "JPEG", quality=92)
                        zf.writestr(f"{Path(nombre).stem}_producto.jpg", buf.getvalue())
                st.download_button(
                    "📦 Descargar todo en un ZIP", buffer_zip.getvalue(),
                    file_name="fotos_producto.zip", mime="application/zip",
                    use_container_width=True,
                )

            with col_b:
                imagenes_b64 = []
                for nombre, im in imagenes_ok:
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=92)
                    b64 = base64.b64encode(buf.getvalue()).decode()
                    imagenes_b64.append((f"{Path(nombre).stem}_producto.jpg", b64))

                items_js = ",".join(f'{{name:"{n}", data:"{b}"}}' for n, b in imagenes_b64)
                html_descarga = f"""
                <button id="btnDescargarTodoIA" style="
                    width:100%; padding:9px 16px; border-radius:8px; border:1px solid #3a3a3a;
                    background:transparent; color:#F2F0EC; cursor:pointer; font-size:0.88rem;
                    font-family:inherit;">
                    ⬇️ Una a una (sin ZIP)
                </button>
                <script>
                const itemsIA = [{items_js}];
                document.getElementById("btnDescargarTodoIA").addEventListener("click", async () => {{
                    for (let i = 0; i < itemsIA.length; i++) {{
                        const it = itemsIA[i];
                        const a = document.createElement("a");
                        a.href = "data:image/jpeg;base64," + it.data;
                        a.download = it.name;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        await new Promise(r => setTimeout(r, 450));
                    }}
                }});
                </script>
                """
                st.components.v1.html(html_descarga, height=46)

            st.caption(
                "El botón 'una a una' hace descargas automáticas seguidas — el navegador puede "
                "pedirte permiso tras la 2ª o 3ª, dale a permitir y sigue solo con el resto."
            )

st.write("")
st.caption(
    "El recorte usa un modelo de IA de segmentación (U2-Net vía rembg), no un simple filtro de "
    "color — funciona con cualquier fondo. La primera foto que proceses tarda un poco más porque "
    "descarga el modelo; las siguientes van rápido."
)
