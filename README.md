# Croma IA — recorte por IA + fondo de producto

App de Streamlit que recorta el fondo de fotos de ropa usando un modelo de
IA de segmentación real (U2-Net, vía la librería `rembg`) y lo sustituye
por un fondo limpio tipo foto de producto profesional. Funciona con
**cualquier fondo original**, no solo verde — no hace falta croma real ni
calibrar nada por foto.

Gratis y sin APIs de pago. Todo corre en el propio servidor de Streamlit.

## Qué hace

- **Recorte por IA**: usa U2-Net (rembg) en vez de un simple filtro de
  color, así que funciona igual de bien con fondos verdes, blancos,
  madera, habitaciones desordenadas, etc.
- **Corrección de "spill" verde**: si el fondo original era croma, quita
  el halo verdoso que queda en los bordes semitransparentes tras
  recortar.
- **Fondos de producto**: blanco liso, gris claro de estudio (ambos con
  degradado sutil tipo papel continuo de fotógrafo), madera, o tu propia
  imagen.
- **Sombra en dos capas**: contacto (pegada a la base) + ambiental
  (difusa), más una sombra de contorno que seguir la silueta exacta de
  la prenda — para que no se vea "pegada" como una pegatina plana.
- **Márgenes y rotación**: margen de aire configurable alrededor de la
  prenda, auto-rotación a vertical, y rotación manual adicional si hace
  falta.
- **Tamaño de lienzo fijo opcional**: todas las fotos de una tanda salen
  con el mismo tamaño y encuadre exactos (varios formatos habituales de
  e-commerce), sin deformar ni recortar la prenda.
- **Realce de color sutil**: contraste, saturación y nitidez suaves,
  como el post-proceso de un fotógrafo de producto.
- **Cámara del móvil integrada**: puedes hacer las fotos directamente
  desde la app, sin subir archivos aparte.
- **Hasta 30 fotos de golpe**, con galería de resultados y descarga en
  ZIP o una a una.

## Instalación

```bash
conda create -n croma-ia python=3.11 -y
conda activate croma-ia
pip install -r requirements.txt
streamlit run app.py
```

La primera vez que proceses una foto se descarga el modelo de IA
(~176 MB); las siguientes van rápido.

## Estructura

```
app.py                     # Interfaz de Streamlit
croma_ia_core.py           # Lógica de recorte, sombras, fondos y color
fondo_madera_default.jpg   # Fondo de madera incluido por defecto
requirements.txt
.streamlit/config.toml     # Tema visual (oscuro, acento dorado)
```
