# Llorca Group · Control económico de obra

Aplicación Streamlit que gira alrededor de la **certificación mensual al cliente** (la venta) y la cruza con las **facturas de proveedores** (el coste) y las **ofertas y contratos** de cada industrial (el compromiso), capítulo a capítulo. Lee facturas con IA, comprueba matemáticamente cada importe y enseña dónde se gana o se pierde margen mientras la obra está viva.

```
        CERTIFICACIÓN (venta)            FACTURAS (coste)              OFERTAS / CONTRATOS
  origen · anterior · mes, por     leídas con IA o a mano,        quién ofertó, quién valoró,
  capítulo y partida, OC,          22 controles, imputadas        a quién se adjudicó, opinión,
  revisiones y opcionales          al mismo capítulo              consumo facturado vs contratado
                 \                         |                              /
                  └──────────────  RENTABILIDAD POR CAPÍTULO  ─────────────┘
                     margen del mes / a origen · alertas · precio venta vs coste por partida
```

El principio es el que pide la propia Dirección de Llorca: **la máquina hace el trabajo repetitivo y prepara la información; la persona valida y aprueba.** La IA solo transcribe. Todas las cuentas las hace Python con aritmética decimal exacta.

## Instalación (Windows + Anaconda)

1. Descomprime la carpeta, por ejemplo en `C:\Users\pc\Downloads\llorca_facturas`.
2. Doble clic en **`instalar.bat`** (solo la primera vez, o tras actualizar la app). Localiza Anaconda aunque no esté en el PATH, crea o actualiza el entorno, comprueba que todas las librerías cargan (incluido el OCR), fija la carpeta `modelos` para Ollama si no hay otra y ofrece descargar el modelo local.
3. Doble clic en **`iniciar.bat`**: activa el entorno, arranca Ollama en segundo plano si está instalado y abre la app en `http://localhost:8501`.

   Los `.bat` solo lanzan `instalar.ps1` / `iniciar.ps1` con `-ExecutionPolicy Bypass`, sin cambiar la política del equipo. Si prefieres hacerlo a mano: Anaconda Prompt → `conda env create -f environment.yml` → `conda activate llorca_facturas` → `streamlit run app.py`.
4. **Primer acceso**: la aplicación pide crear el usuario **administrador**. Desde Configuración se dan de alta los demás:
   - **Administrador**: acceso completo, incluidos los usuarios y la configuración.
   - **Gestor**: facturas, certificaciones, contratación y maestros.
   - **Consulta**: solo lectura.

   Detalles de seguridad:
   - Las contraseñas se guardan cifradas (PBKDF2-SHA256 con sal por usuario).
   - Tras 5 intentos fallidos el usuario se bloquea 10 minutos.
   - Cada acceso queda en la auditoría.
5. **Servidor para toda la oficina**: doble clic en **`servidor.bat`**. La primera vez, con clic derecho → «Ejecutar como administrador», para abrir el puerto 8501 en el cortafuegos.
   - La ventana muestra la dirección de acceso, p. ej. `http://192.168.1.20:8501`.
   - Todos los equipos de la red trabajan sobre la misma base de datos y ven lo mismo en tiempo real.
   - **Sesiones persistentes**: recargar la página no obliga a entrar de nuevo (12 h, o 30 días con «Mantener la sesión»).
   - El administrador ve las sesiones abiertas y puede cerrarlas. Al cambiar la contraseña de un usuario o desactivarlo, se cierran sus sesiones.
6. Listo: la lectura de facturas es local por defecto. Con Ollama se activa el agente local (Qwen3-VL + reglas + memoria); sin Ollama sigue funcionando OCR + reglas. No hace falta ninguna API de pago. Para probarla sin gastar nada: **📥 Entrada de facturas → «Cargar demo verificada»** (9 facturas reales de la obra 664 transcritas y comprobadas a mano).

## Pantallas

| Sección | Para qué |
|---|---|
| 🏠 Inicio | Cifras clave de la obra y lista de pasos pendientes con acceso directo |
| 💰 Rentabilidad | Venta vs coste vs contratado por capítulo; modo mes / a origen / periodo; alertas; detalle de capítulo (facturas, ofertas, partidas certificadas, responsable); evolución mensual; relacionar facturas; precio de venta vs coste; costes manuales / SIS |
| 📊 Coste de obra | Coste por partida, proveedor (Pareto), mes, matriz partida × proveedor, extras, retenciones, vencimientos, histórico de precios, Excel |
| 📑 Certificaciones | Importación verificada del PDF, resumen por capítulos, explorador de las 681 partidas, comparación entre certificaciones, estructura de coste |
| 🤝 Contratación | Ofertas por capítulo, comparativa con dispersión y venta prevista, adjudicación, opiniones y valoración, consumo de contratos |
| 💬 Asistente IA | Preguntas en lenguaje natural sobre todo lo anterior |
| 📥 🔍 🧾 ⚠️ | Circuito de facturas: buzón de correo automático, entrada (IA, carpeta, ZIP o a mano), revisión con aprobación masiva de lo limpio, pagos, incidencias |
| Ejecución de obra | Certificación a subcontratas, preparación de la certificación al cliente, planificación, planos y fichas técnicas, Seguridad y Salud, actas desde audio |
| Finanzas | Conciliación bancaria, contabilidad en SIS por API, correo saliente |
| Automatizaciones | Buzón, correo, SIS, copias de seguridad y estado de las tareas en segundo plano (administrador) |

## Fiabilidad frente a errores de uso

**Al subir**, se rechaza con el motivo:
- archivos vacíos;
- «PDF» que no lo son (extensión cambiada);
- PDF con contraseña, dañados o de cientos de páginas.

**Duplicados**, en todos los niveles:
- **Mismo archivo**: no entra.
- **Mismo contenido con otro nombre**: queda como «Duplicado» y no computa.
- **Mismo emisor, número e importe**: queda como «Duplicado».
- **Certificaciones**: mismo archivo ya importado (bloqueado), mismo importe a origen con otro número (aviso), certificación de otra obra (aviso).
- **Obras**: nombre o alias muy parecidos a otra existente (pide confirmación).
- Tras cada subida se informa de lo que ya existía y no se ha añadido.

**Obra al subir** (opcional):
- **Sin marcar «Forzar»**: manda la obra que indique cada factura, y la elegida se usa si la factura no dice ninguna.
- **Con «Forzar»**: todas van a la elegida.
- Queda incidencia si una factura indica otra obra distinta a la del lote o menciona varias.

**Documentos equivocados**:
- Una certificación a cliente subida como factura se detecta y no computa como coste.
- Un PDF sin importes ni NIF se marca como «No parece una factura».

**Deshacer (todo tiene vuelta atrás)**:
- **Versiones de cada factura**: antes de cualquier cambio de cabecera, líneas, IVA, estado o relectura se guarda una foto completa. En Revisión → Historial se vuelve a cualquier versión; la actual se guarda antes, así que también se puede volver a ella.
- **Página «Papelera y deshacer»**:
  - documentos en papelera (restaurar; el borrado definitivo solo lo hace el administrador);
  - certificaciones y obras eliminadas (se guardan íntegras y se restauran con sus mismos datos);
  - estructura de partidas anterior a «usar capítulos de la certificación»;
  - fusiones de proveedores;
  - **deshacer una subida entera** o **una lectura en lote entera**;
  - últimas 300 acciones.
- **Reabrir incidencias** ya resueltas.
- **Fusionar proveedores duplicados**, con detección automática de parecidos. Avisa si tienen NIF distinto.
- **Eliminar obras** solo si no tienen documentos ni certificaciones; si no, se pueden desactivar.

**Protección frente a errores de uso**:
- Si dos personas editan la misma factura a la vez, no se pisan: la segunda recibe un aviso y ve los datos actuales.
- Importes con letras o formato inválido: no se guarda nada y se indica qué campo.
- Marcar como pagada una factura no aprobada: aviso.
- Las acciones irreversibles piden confirmación.
- Todo queda en la auditoría.

**Confianza**: es la suma de comprobaciones objetivas:
- los importes cumplen las ecuaciones;
- la base lleva su etiqueta;
- CIF válido;
- número, fecha y nombre del emisor;
- las líneas suman la base;
- a nombre de la empresa;
- coincide con lo aprendido del proveedor.

Si no llega a 0,85, se hace una **segunda pasada** con otras extracciones del mismo PDF (texto plano, motor alternativo y OCR si hace falta) y se queda la mejor.

## Cargos, permisos y circuito de aprobación

| Cargo | Qué ve y qué hace |
|---|---|
| Administrador | Todo, incluidos usuarios, sesiones, reglas de aprobación y configuración |
| Dirección | Todas las obras; aprueba facturas de cualquier importe |
| Administración / Finanzas | Circuito de facturas completo, pagos, proveedores y certificaciones; aprueba hasta el umbral |
| Jefe de obra | Solo sus obras (asignadas en Obras y partidas): da la **conformidad** a sus facturas y consulta rentabilidad, certificaciones y contratación |
| Técnico / Estudios | Certificaciones, contratación, obras y partidas; consulta de costes |
| Consulta | Solo lectura |

Circuito, configurable en Configuración:
1. Administración revisa los datos.
2. El jefe de obra da la conformidad (en obras con jefe asignado).
3. Se aprueba. Desde el umbral fijado (por defecto 10.000 €) solo aprueba Dirección.

Reglas del circuito:
- Si alguien modifica la factura después, la conformidad y la aprobación se anulan solas.
- **Mis pendientes** muestra a cada persona su bandeja (conformidad, revisión, aprobación, Dirección), con días de espera y aviso de retraso.

## Comprobaciones económicas y contables adicionales

- **Albarán facturado dos veces**: el mismo albarán en dos facturas del mismo proveedor es crítico (posible doble cobro).
- **Precio fuera de lo habitual**: una línea más del 10 % por encima de la mediana de ese artículo y proveedor, con el sobrecoste en euros.
- **IVA según el tipo de operación**: aviso si una ejecución de obra lleva IVA en lugar de ISP, o si un suministro de material lleva ISP.
- **Sociedades del grupo**: la factura debe ir a nombre de una sociedad configurada; se avisa si el nombre del cliente no casa con el NIF (p. ej. «Josep Llorca Construcciones» con el NIF de Llorca Group).
- **Rectificativas** sin la factura original y **facturas fuera de su mes** (afectan al cierre).
- **Coste devengado sin facturar**: certificaciones y albaranes de proveedor que aún no tienen factura. Se muestran como provisión de cierre y, opcionalmente, se suman al coste en la rentabilidad para no sobrestimar el margen.
- **Presupuesto de coste de Estudios** por capítulo (importado en Obras y partidas): coste esperado según el avance y desviación frente a Estudios.
- **Cuenta contable propuesta** en cada factura (subcontratas 607, materiales 601, alquileres 621, profesionales 623, transportes 624…), aprendida del histórico aprobado de cada proveedor.
- **Avales y garantías**: vencimientos, comisión anual que sigue corriendo y aviso de avales vencidos (los físicos hay que recuperarlos).

## Buscador y asistente

- **Buscar** (menú superior): un único cuadro que encuentra facturas, líneas, proveedores, partidas certificadas y ofertas por texto, CIF, número o importe (p. ej. «8509,52»).
- **Asistente**:
  - **Respuesta inmediata**: las preguntas habituales (coste, partidas, proveedores, retenciones, vencimientos, incidencias, margen, certificaciones, ofertas, «cuánto en papel pintado…») se traducen directamente a consultas y se contestan con las cifras exactas en menos de un segundo.
  - **Redacción opcional**: se puede activar que la IA local redacte la respuesta; tarda más en CPU.
  - La conversación se guarda por usuario: sobrevive a recargas y a otros equipos.
  - Detecta qué sabe hacer el modelo local. Si no admite herramientas, como algunos modelos Vision, usa directamente el modo de consultas planificadas en lugar de fallar.
  - Sin ningún modelo instalado, ofrece consultas directas.

## La certificación

El PDF de certificación de SIS (Crystal Reports, columnas ORIGEN · ANTERIOR · ACTUAL) se lee **sin IA**, de forma determinista, y se verifica con su propia aritmética:

- en cada partida, origen = anterior + actual (importe y cantidad) y cantidad × precio = importe;
- en cada capítulo y subcapítulo (anidados hasta 4 niveles), Σ partidas = total impreso;
- en el documento, Σ capítulos = «Totales».

Además del PDF, se admite la certificación en **Excel o CSV**: la exportación de SIS con marcas OR/AN/AC, o cualquier tabla con código, descripción e importe (Presto, hojas propias).
- Los capítulos se reconocen por sus filas «Total Capítulo».
- Si la hoja solo trae el importe a origen, el «anterior» de cada partida se toma de la certificación previa de la obra. Se emparejan por código, precio y título, y también se reconocen códigos con espacio o sufijo («DSFV ER», «14.14(1)»).
- Comprobado con la hoja de agosto: los 24 capítulos coinciden con el PDF y el anterior y el mes salen exactos (5.960.578,621 € y 240.016,895 €).

Con la certificación nº 21 de Alibuilding (68 páginas) se leen **681 partidas y 134 capítulos/subcapítulos, con 0 descuadres**: 6.200.595,516 € a origen, 5.960.578,621 € anterior y 240.016,895 € en el mes. Los importes se guardan en milésimas de euro, como vienen en el documento.

Casos especiales que se tratan:
- partidas de medición sin precio (incluidas en otra);
- partidas anuladas (origen 0, descertificadas en el mes);
- códigos repetidos en capítulos distintos;
- capítulos de naturaleza distinta: contrato, 569 Opcionales, 22 Revisión y 571 Órdenes de cambio (cada OC con su código).

Al importar la siguiente certificación:
- **Encadenado:** se comprueba que el «anterior» de cada partida coincide con el «origen» del mes previo.
- **Comparación:** se listan partidas nuevas, eliminadas, cambios de precio, cambios de medición prevista y partidas descertificadas, con su impacto en euros.

La **venta prevista** de cada partida se deduce de su «% a origen» (cantidad origen ÷ %).

**Estructura de coste**: un botón sustituye las partidas genéricas de la obra por los capítulos de la certificación y reclasifica las facturas. Así venta y coste se comparan con la misma estructura.

## Auditoría mensual (método del departamento)

Reproduce, con datos brutos y sin fórmulas frágiles, la hoja «ALIBUILDING AGOSTO CRITERIOS JULIO».

**Datos de entrada**
- Certificación del cliente (PDF).
- Criterios que asignan cada partida a una categoría.
- Compras de SIS, con cada proveedor asignado a una categoría y un tipo (directo o indirecto).
- Partes de trabajo de SIS (personal propio).
- Parámetros de la obra: pase, presupuesto, CD, CI, GG, indirectos y desviación prevista.

**Cálculo**
- **Resultado SIS** = ventas a origen − compras − personal.
- **Por categoría**:
  - cobrado sin pase = cobrado ÷ (1 + pase);
  - diferencial = sin pase − pagado.
- **Corrección** = − Σ diferencial de las categorías **en curso**. Las terminadas no se corrigen.
- **Resultado corregido** = resultado SIS + corrección.
- **Proyección** = resultado corregido + beneficio por indirectos − desviación prevista + GG de la facturación pendiente.

**Verificado contra la hoja de agosto**
- Ventas 6.200.595,52 €, compras 4.487.950,09 €, personal 387.446,00 €.
- Resultado SIS 1.325.199,43 €, corrección −901.274,76 € y resultado 423.924,67 € (6,84 %): idénticos al céntimo.
- `tests/test_auditoria.py` lo comprueba.

**Error detectado en la hoja original**
- Las celdas D38 y D42 de la proyección apuntan a la columna de **abril** (Q) y no a la de agosto (U).
- Por eso la hoja da una proyección de 331.698,95 € (5,28 %). Con agosto, la proyección correcta es **340.634,71 € (5,42 %)**.
- La app calcula siempre con el último mes y avisa de este error al importar la hoja.

**Qué añade frente al Excel**
- **Controles de calidad**: % de venta y de compras clasificadas, partidas sin criterio, proveedores sin categoría.
- **Partidas nuevas del mes**: se proponen con la categoría mayoritaria de su subcapítulo.
- **Estado de cada categoría** (en curso o terminada) editable.
- **Cierre del mes** en el histórico, con gráficos de evolución y margen.
- **Facturas aún no contabilizadas**: opción de sumar las registradas en la app que todavía no están en SIS.
- **Informe Excel** con fórmulas vivas (Auditoría, Categorías, Evolución), verificado sin errores de fórmula.

Cada mes basta con importar la nueva certificación en PDF y las exportaciones de SIS de compras y de partes de trabajo, que se reconocen solas por sus columnas. Después se revisan las partidas nuevas.

## Rentabilidad: cómo leerla

- **Mes de la certificación** (por defecto): lo certificado en el mes frente a las facturas con fecha entre la certificación anterior y esta. Es la comparación honesta cuando solo están cargadas las facturas recientes.
- **A origen**: todo lo certificado frente a todo el coste cargado. La app avisa si el coste cubre poco periodo (con 100 facturas de dos meses frente a 21 certificaciones, el margen a origen saldría falsamente altísimo). Para usarlo bien, importa el coste a origen desde SIS en «Costes manuales / SIS».
- **Alertas**:
  - coste por encima de lo certificado;
  - facturado por encima de lo contratado;
  - coste sin nada certificado en el periodo;
  - coste sin capítulo de venta;
  - contratado por encima de la venta prevista.
- **Relacionar facturas**:
  - cada línea de factura se imputa a un capítulo;
  - opcionalmente también a una partida concreta de la certificación, sugerida por similitud de texto ponderada por rareza de las palabras (IDF);
  - con eso se compara precio unitario de coste frente a precio de venta.

## Modos de trabajo

- **Automático**: IA lee el lote → controles → sugerencias de capítulo y partida → aprobación masiva *solo* de lo que supera todos los controles.
- **Asistido**: la IA propone y la persona corrige en la revisión (PDF al lado) o en la tabla de imputación masiva.
- **Manual**: alta de facturas sin PDF, ofertas, costes propios o importados de SIS, responsables y notas por capítulo. Todo funciona sin API key salvo la lectura de facturas y el asistente.

## Flujo de trabajo

1. **📥 Entrada de facturas**: sube PDFs, un ZIP (como `AlibuildingFacturas_zip.zip`) o indica una carpeta. Cada PDF se identifica por su huella SHA‑256: el mismo archivo nunca entra dos veces.
2. **Leer con IA local**: Qwen3-VL recibe las páginas relevantes como imágenes y el texto extraído como apoyo; devuelve un JSON con esquema fijo. En documentos difíciles se hace una segunda lectura Vision independiente. Claude queda únicamente como motor opcional de nube.
3. **Validación automática** (sin IA): se ejecutan 22 controles y se generan incidencias.
4. **🔍 Revisión y aprobación**: PDF a la izquierda, datos a la derecha. Se corrigen cabecera, líneas, partidas e IVA; las incidencias se resuelven con justificación obligatoria. **No se puede aprobar con incidencias críticas o altas abiertas.** Si se modifica un documento ya aprobado, la aprobación se anula automáticamente.
5. **📊 Panel de obra**: coste por partida frente a presupuesto, Pareto de proveedores, evolución mensual, matriz partida × proveedor, extras, retenciones de garantía, vencimientos, histórico de precios y exportación a Excel.
6. **💬 Asistente IA**: preguntas en lenguaje natural («¿cuánto llevamos en solados en la 664?», «¿hay cambios de IBAN?»). Las cifras salen de funciones deterministas; la IA no suma nada, y cada respuesta enseña las consultas que ha hecho.

## Cómo se leen las facturas (100 % en tu ordenador)

Por defecto **ningún documento sale del equipo**:

1. **Texto**: capa de texto del PDF; si es escaneado, OCR en CPU (RapidOCR, incluido en pip).
2. **Reglas matemáticas** (siempre, menos de un segundo por factura):
   - NIF/CIF e IBAN con dígito de control.
   - Número, fecha y vencimiento, también cuando van en tabla de cabecera (etiqueta arriba, valor debajo).
   - Solver de importes: base × tipo = cuota, base + cuota = total, total − retención = líquido. Prima los importes mayores y descarta combinaciones con cantidades pequeñas.
   - Líneas de detalle que cierran cantidad × precio × (1 − descuento). Admite el descuento como columna sin «%», el precio desplazado a la línea de abajo y facturas de varias páginas con «suma y sigue».
   - Conceptos de pie que completan la base (servicios, palets, portes).
3. **Agente local (Ollama)**: Qwen3-VL (`qwen3-vl:4b-instruct`) se usa para visión/documentos; las reglas matemáticas siguen siendo la autoridad para importes. En PDFs escaneados se envían hasta 8 páginas relevantes; en PDFs con texto también se hace control visual cuando la IA interviene.
   - Si la primera lectura tiene descuadre, conflicto o el documento es escaneado, se hace una **segunda lectura Vision**. Si ambas coinciden en los importes clave, la confianza sube; si no, queda marcado para revisión.
   - El cliente Ollama reintenta con contexto menor y CPU si una configuración falla, en vez de tumbar la lectura.
4. **Base de conocimiento local**: cada factura aprobada se indexa automáticamente en SQLite + Chroma y, si está disponible, con `qwen3-embedding:0.6b`. Las siguientes facturas recuperan ejemplos similares del mismo proveedor/obra para reconocer formatos y patrones. Los ejemplos nunca pueden inventar datos de la factura actual.
5. **Sinónimos y formatos**:
   - Etiquetas en castellano, catalán e inglés (base imposable, net amount, invoice number, invoice date…).
   - IVA general y reducidos, IGIC.
   - Fechas en «31/08/2026», «31.08.2026», «2026-08-31», «31 de agosto de 2026» y «agosto 31, 2026».
6. **El emisor nunca puede ser la propia empresa**: si la IA o un documento confuso proponen a LLORCA como proveedor, se descarta y queda una incidencia crítica.
7. **Duplicados**:
   - Mismo archivo (huella SHA-256): no entra.
   - Mismo contenido con otro archivo (reenvíos, reexportaciones): se registra como «Duplicado» y ni se lee ni computa.
   - Mismo emisor, número e importe tras la lectura: se marca «Duplicado» automáticamente.

**Lectura en segundo plano** (con porcentaje, tiempo medio por documento, tiempo restante estimado y el documento que se está leyendo en cada momento). En cada lote se elige el uso de la IA local:
- **solo cuando falten datos** (recomendado);
- **nunca**: máxima velocidad, solo reglas + OCR;
- **siempre**: doble contraste, lento en CPU.

La IA solo recibe los campos que faltan y un texto recortado, así que tarda bastante menos que antes.
- **Continuidad**: al pulsar «Leer», el lote se encola en la base de datos y lo procesa un lector interno. Se puede cambiar de pantalla o cerrar el navegador, y si la aplicación se reinicia continúa donde se quedó.
- **Seguimiento**: el progreso aparece en la barra lateral de todas las páginas, y el detalle por documento en Entrada de facturas: proveedor, nº, base, confianza, tiempo y errores. Se puede cancelar.
- **Filtros y selecciones**: se conservan al cambiar de página.

**Formatos admitidos**:
- PDF con texto o escaneado.
- Fotos y escaneos (JPG, PNG, TIFF multipágina, BMP, WEBP): se convierten a PDF respetando la orientación del móvil.
- ZIP, incluso anidados.
- Correos .eml y .msg de Outlook: se extraen los adjuntos y se descartan logotipos y firmas.
- Importación desde una carpeta completa.

**Campos que se leen de cada línea**
- Descripción, cantidad, precio, descuento e importe.
- Unidad:
  - la de su columna, si existe;
  - si no, la que indica la descripción («ML», «M2», «M3»);
  - si no, se deduce: m² en revestimientos con decimales, kg en acero, m en tubos, h en mano de obra y «ud» en cantidades enteras.
- Facturas de servicios sin cantidad ni precio: se leen como líneas de concepto e importe, solo si un bloque de ellas suma exactamente la base.
- Albarán y fecha del albarán.
- Tipo de línea: normal, portes o palets, descuento, anticipo.

**Partidas de cada línea**
- Vocabulario de obra (morteros, áridos, ferralla, impermeabilizantes, palets…), con raíces de palabra.
- Si una línea no encaja: la partida habitual de ese proveedor, aprendida de facturas anteriores, o la dominante de la propia factura. Queda marcada para revisar.

### Resultados medidos con las facturas reales de la obra 664 (solo reglas, sin IA)

- **9 facturas verificadas a mano**: 54 de 54 campos correctos (base, total, líquido, NIF, fecha y número). Incluye la base con anticipo deducido de Aquatech, la factura a 0 € de Dolz y la retención impresa en negativo.
- **87 PDF con texto**:
  - Importes resueltos en 82. Los 5 restantes: 2 son partes de horas (no son facturas) y 3 tienen formatos que requieren revisión.
  - Cerrados por ecuación: 39 por IVA y 23 por retención.
  - Número de factura en 74, fecha en 83 y NIF del emisor en 65.
  - Tiempo: ≈0,3 s por factura.
- **Escaneados (OCR)**: importes leídos; fecha y nombre del emisor peor. Siempre llevan la marca «documento_escaneado_OCR» para revisión obligatoria.

Lo que el motor no sabe se queda vacío y genera incidencia: nunca rellena a ciegas.

### Instalar el agente IA local (sin API)

1. Instala **Ollama para Windows**.
2. Ejecuta `instalar.bat`: ofrece descargar estos modelos:
   - `qwen3-vl:4b-instruct` → lectura visual de facturas/documentos.
   - `qwen3:4b-instruct` → asistente y razonamiento textual.
   - `qwen3-embedding:0.6b` → memoria semántica local.
3. En ⚙️ Configuración → Motor de lectura puedes probar Vision, reconstruir la memoria y cambiar modelos.
4. Si el equipo va justo de RAM, usa `qwen3-vl:2b-instruct` como modelo Vision. No hay tokens, cuotas ni API: el límite práctico es el hardware local.

Cómo afecta al funcionamiento:
- **Velocidad**: en CPU tarda del orden de medio minuto a un par de minutos por factura; con GPU, bastante menos.
- **Asistente**:
  - **Respuesta inmediata**: las preguntas habituales (coste, partidas, proveedores, retenciones, vencimientos, incidencias, margen, certificaciones, ofertas, «cuánto en papel pintado…») se traducen directamente a consultas y se contestan con las cifras exactas en menos de un segundo.
  - **Redacción opcional**: se puede activar que la IA local redacte la respuesta; tarda más en CPU. el mismo modelo sirve para el 💬 Asistente conversacional, también en local. Sin Ollama, el asistente ofrece consultas directas sin IA.
- **Espacio en disco**: Ollama guarda los modelos en su carpeta habitual. `instalar.bat` mantiene además la posibilidad de usar `OLLAMA_MODELS` en otra unidad.

La API de Claude sigue disponible como motor opcional en la nube, pero está desactivada por defecto.

## Garantías matemáticas

- **Ningún importe pasa por `float`.** Se trabaja con `decimal.Decimal` y se guarda en **céntimos enteros** en SQLite. Cantidades, precios y porcentajes se guardan como texto decimal exacto.
- La IA devuelve los números como cadenas (`"1234.56"`) validadas por patrón; se le prohíbe calcular.
- Redondeo `ROUND_HALF_UP` a céntimo, por tipo impositivo.
- **Invariante contable comprobado siempre en el panel**: Σ coste por partidas = Σ bases imponibles. Si en una factura las líneas no suman la base (descuentos o anticipos globales), la diferencia se imputa a «99 · Sin asignar» como «Ajuste de cuadre»: nunca se pierde ni se inventa un céntimo.
- El coste de obra se mide por **base imponible** (el IVA soportado es deducible; con inversión del sujeto pasivo es neutro). Anticipos y su posterior deducción se compensan exactamente (caso Dolz FP260300 → FP260313, caso Aquatech 2026/35 → 2026/40).

### Tolerancias y por qué

| Control | Tolerancia | Justificación |
|---|---|---|
| Base + IVA + recargo = total; total − retenciones = a pagar; Σ líneas = base | ±0,02 € | Cada importe impreso tiene hasta 0,005 € de error de redondeo; al encadenar tres, máx. 0,015 € |
| Cuota IVA = base × tipo | ±0,01 € | Redondeo por tipo impositivo (RD 1619/2012). Si el emisor redondea por línea (hasta n × 0,005 €) se informa como «info», no como error |
| Cantidad × precio × (1 − dto) = importe | \|cantidad\| · 0,5·10⁻ᵖ + 0,005 € | p = decimales del precio impreso |

## Controles automáticos

| Código | Control |
|---|---|
| C01 | Σ líneas = base imponible (distingue bruto vs. base con descuento/anticipo) |
| C02 / C03 | Cuota de IVA y recargo de equivalencia por tipo; Σ bases IVA = base |
| C04 | Base + IVA + recargo = total factura |
| C05 / C06 | Retención de garantía e IRPF bien calculados (y sobre qué base) |
| C07 | Total − IRPF − garantía = líquido a pagar |
| C08 | ISP sin IVA; factura sin IVA sin causa de exención ni ISP |
| C09 | NIF/CIF/NIE del emisor con dígito de control oficial |
| C10 | Factura dirigida al NIF de la empresa (B54727722, configurable) |
| C11 | IBAN válido (módulo 97 + CCC) y **cambio de cuenta bancaria del proveedor** (patrón típico de fraude) |
| C12 | Duplicados: mismo proveedor + nº normalizado; mismo proveedor + fecha + importe |
| C13 | Fechas: ausentes, futuras, vencimiento anterior a la emisión |
| C14 | Obra no identificada o con baja confianza |
| C15 | Cálculo de cada línea |
| C16 | **Anclaje al PDF**: la base y los totales leídos por la IA deben aparecer literalmente en el texto del PDF |
| C17 | Signo incoherente (abono positivo, factura negativa) |
| C18 | Confianza baja, campos dudosos y observaciones de la IA |
| C19–C22 | Líneas sin partida, nº de factura ausente, documento soporte no computable, subcontrata sin retención |

## Identificación de obra y partidas

- **Obra**: primero el código literal («664» como palabra completa, sin confundirlo con importes), luego los alias exactos y, por último, una similitud difusa tolerante a erratas (ALIBULDING / ALIBUIDING). Los alias se editan en **🏗️ Obras y partidas**.
- **Partidas**: cada obra nace con 17 capítulos estándar de edificación. La IA propone la partida de cada línea con su confianza; si duda, actúa un clasificador por palabras clave. El usuario tiene siempre la última palabra.
- **Presupuesto**: se importa desde Excel o CSV (`codigo; descripcion; presupuesto_coste; presupuesto_venta; palabras_clave`) con la plantilla descargable. Así el panel muestra desviación y % consumido por partida.

## Arquitectura

```
app.py                 navegación y barra lateral
core/
  money.py             aritmética exacta, parseo de importes ES/EN, formato €
  fiscal.py            NIF/CIF/NIE e IBAN/CCC con algoritmo oficial
  db.py                esquema SQLite (céntimos) y auditoría
  extractor.py         lectura opcional con Claude (nube)
  extractor_local.py   reglas + OCR + Qwen3-VL + segunda lectura + fusión segura
  knowledge_base.py    memoria local SQLite + Chroma + Qwen3 Embedding
  validation.py        los 22 controles deterministas
  maestros.py          obras, partidas, proveedores, identificación y clasificación
  ingesta.py           alta, aplicación de la lectura, edición y circuito de aprobación
  analytics.py         coste por obra/partida/proveedor/mes, retenciones y vencimientos
  certificacion.py     lector determinista y verificación de certificaciones (ORIGEN/ANTERIOR/ACTUAL)
  obra_control.py      certificaciones, estructura de coste, ofertas, rentabilidad, comparación, relación factura↔partida
  agente.py            asistente con herramientas deterministas
  export.py            Excel con formato contable
  esquema.py           creación única de todas las tablas (app y pruebas)
  planificador.py      tareas automáticas en segundo plano con turno en la base de datos
  credenciales.py      contraseñas cifradas (Fernet) y OAuth2 de Microsoft 365
  buzon.py             lectura IMAP del buzón, clasificación de adjuntos, aprendizaje del remitente
  respaldo.py          copia de seguridad verificada, espejo de archivos, rotación y restauración
  correo.py            bandeja de salida con aprobación y envío SMTP
  sis.py               asientos a SIS por API REST (idempotente, modo prueba)
  cert_proveedor.py    contratos, certificación a subcontratas y autorización de facturación
  cert_cliente.py      propuesta de certificación al cliente desde lo medido a subcontratas
  planificacion.py     calendario laboral, camino crítico, línea base y replanificación
  planos.py            revisiones de planos y fichas, aprobación de la DF y distribución
  prevencion.py        CAE, control de acceso y trazabilidad de residuos
  conciliacion.py      Norma 43 / Excel y casación con remesas, pagos y cobros
  actas_audio.py       transcripción local (Whisper) y paso al diario de obra
  pdf_simple.py        PDF sin dependencias para los documentos que emite la app
vistas/                una página Streamlit por módulo
tests/                 casos reales verificados y prueba del núcleo
demo/                  los 9 PDF reales de la demo
data/                  base de datos y PDFs archivados (se crea al arrancar)
```

Pruebas automáticas: `python -m pytest` desde la carpeta de la aplicación. Se ejecutan solas en cada cambio (GitHub Actions,
`.github/workflows/llorca-pruebas.yml`) y cubren buzón, copias, correo, SIS, certificación a subcontratas y al cliente,
planificación, planos, CAE y residuos, conciliación, actas, tesorería, pagos SEPA, cierres y estudios, más un recorrido de todas
las pantallas nuevas con tres cargos distintos. Cada prueba usa una base de datos nueva y documentos sintéticos.

Los scripts con los documentos reales de la obra 664 (`tests/test_core.py`, `test_obra.py`, `test_auditoria.py`) se lanzan desde
pytest si los PDF están en `demo/` (no se publican en el repositorio porque contienen NIF e IBAN de terceros; ver `demo/LEEME.md`).

## Datos, privacidad y dependencia de terceros

- Con el motor local (por defecto) no se envía nada a ningún servicio externo: OCR, reglas, IA local y base de datos corren en el propio equipo.
- Todo se guarda en local: `data/llorca_facturas.db` y `data/pdfs/`. La copia de seguridad es automática y verificada (ver «Copia de seguridad automática»).
- A la API de Anthropic solo se envía el PDF de cada factura para su lectura, más la lista de obras y partidas, y las preguntas del asistente junto con los resultados agregados de las consultas. Las condiciones de uso y retención de datos de la API están en https://www.anthropic.com/legal.
- El modelo es configurable. El código, la base de datos y las reglas son de quien ejecuta la aplicación; si se deja de usar la IA, todo lo registrado sigue disponible y se puede seguir trabajando en modo manual.

## Limitaciones conocidas y siguientes pasos

- **PDFs con varias facturas** en un mismo archivo: se extrae la factura principal y se listan los albaranes. Si un PDF agrupa varias facturas, conviene separarlo.
- **Venta prevista**: se deduce del % a origen. Las partidas con 0 % no aportan previsión. Si se dispone del presupuesto de contrato (BC3/Presto), conviene importarlo por capítulos en «Obras y partidas».
- **Imputación a capítulo**: la clasificación automática es una propuesta (reglas + IA). Revísala en «Relacionar facturas» antes de sacar conclusiones de margen.
- **Lectura**: el detalle de líneas sale en unas 60 de 87 facturas; el CIF que solo aparece en el logotipo y los escaneados siguen necesitando revisión la primera vez (después, el remitente del correo y el IBAN verificado identifican al proveedor).
- **Fuera de este sistema**, por decisión: doble factor de acceso, paso a PostgreSQL (SQLite basta para una oficina) y los procesos de Administración que viven en A3 o Bizneo (flota, viajes, reconocimientos médicos, liquidaciones de apartamentos y hotel).

### Pendiente de comprobar en la instalación real

Todo lo nuevo está probado con pruebas automáticas y servidores simulados, pero hay piezas que dependen de sistemas externos:

- **Buzón**: conexión con el servidor real (Gmail con contraseña de aplicación, o Microsoft 365 con una aplicación registrada con el permiso `IMAP.AccessAsApp`). Botón «Probar conexión» en Automatizaciones.
- **Correo saliente**: SMTP real. Empezar en modo PRUEBA (genera .eml) y pasar a real cuando los textos estén bien.
- **SIS**: la API de SIS de cada instalación es distinta. Se envía un JSON con mapeo de campos configurable; hay que validarlo con el proveedor de SIS en modo PRUEBA (`data/sis_prueba/`) antes del modo real.
- **Conciliación**: extracto Norma 43 real del banco (la lectura se verifica con los propios totales del fichero) y la remesa SEPA en el banco.
- **Actas**: `faster-whisper` en el servidor (instalar.bat lo intenta); la primera transcripción descarga el modelo.
- Los modelos de Ollama, la sesión recordada en un navegador de verdad, BC3 reales de Presto y el acceso desde otros equipos de la red.

## Novedades de la versión 1.x (programa J3)

### Costes y ventas internos (solo uso interno; el cliente no los ve nunca)
- **Qué recoge**: lo que no llega en factura de proveedor pero es coste o venta de la obra.
  - Costes: mano de obra propia (horas × coste/hora por categoría), personal técnico, maquinaria y medios propios, instalaciones y casetas, seguros y avales, licencias y tasas, consumos, gastos generales imputados y posventa.
  - Cargos a subcontratas (limpieza, grúa, penalizaciones): **restan** coste.
  - Ventas internas: obra ejecutada pendiente de certificar, revisión de precios y extras aprobados.
- **Justificantes**: cada movimiento lleva los suyos adjuntos (PDF, Excel, Word, fotos, correos…).
- **Validación**: la hace Administración o Dirección, nunca quien lo creó (cuatro ojos). Sin justificante no se valida (configurable). Nada se borra: se anula con motivo.
- **Partes de trabajo**: se importan en Excel/CSV (fecha, horas, trabajador, categoría…), con control de horas absurdas y el archivo como justificante.
- **Reparto de gastos generales** del mes entre obras: por venta certificada, por coste o a partes iguales, con el cálculo explicado.
- **Dónde cuentan**: en la rentabilidad por capítulo (columna «Venta interna») y por oficio (la mano de obra en personal propio), y en los informes de Dirección y Finanzas.

### Casación triple y excepciones
- **Qué comprueba**: en las subcontratas con contrato adjudicado, cada factura debe casar con una certificación del proveedor (misma base ±1 %) y caber en lo contratado.
- **Excepciones**: lo que no casa aparece con el descuadre explicado en euros.
- **Dinero colgado**: anticipos pagados aún no descontados.
- **Indicador**: % de casación sin excepción.

### Tesorería y cobros
- **Factura emitida desde la certificación**: importe del mes, serie SII, IVA y retención configurables. Una sola factura por certificación.
- **Numeración y emisión**: correlativa por serie y año al emitir (borrador → emitida; exige NIF del cliente válido). Se imprime a PDF desde el navegador.
- **Cobros**: parciales o totales, sin permitir cobrar más de lo pendiente.
- **Aging** por tramos y por cliente.
- **Retenciones de garantía del cliente** (cuentas 430/4308):
  - radar con vencimiento a 365 días (configurable) y aviso previo;
  - borrador de reclamación;
  - registro de quién reclamó, cuándo y qué respondió el cliente;
  - alta de facturas históricas para el inventario.
- **Previsión de caja semanal**: cobros esperados frente a pagos aprobados, con aviso de saldo negativo.

### Estudios y ofertas
- **Medición**: en **BC3 (FIEBDC-3)** o Excel, normalizada al árbol de partidas.
- **Corte por industrial**: propuesto automáticamente; el técnico lo confirma.
- **Separata en Excel** por industrial, registro de a quién se pidió oferta (sin duplicados) y correo preparado.
- **Lectura de las ofertas** recibidas (la propia separata rellenada): detecta huecos y códigos alterados.
- **Comparativa homogénea**: partida a partida, con condiciones y la referencia del **histórico propio**.
- **Archivo de coste con huecos**: mejor oferta → histórico propio → hueco. El precio de venta lo fija Estudios.

### Otros
- **Cuadro de mando** con los indicadores de cada agente:
  - facturación: volumen procesado, % de casación, tiempo hasta la aprobación, aprobadas a la primera;
  - tesorería: demora de cobro, retenciones y avales vigilados;
  - estudios: tiempo por estudio y ofertas por compra.
- **Asientos contables** de las facturas aprobadas (gasto, IVA, ISP, IRPF, retención y proveedor), cuadrados y con la ruta del PDF, en Excel o CSV para SIS. Cuentas configurables.
- **Búsqueda de texto** dentro de todos los PDF: los documentos antiguos se indexan solos en segundo plano.
- **Informes mensuales** por destinatario y **diario de obra**, que ahora incluye incidencias de **posventa**, **asuntos jurídicos** y **planos/fichas técnicas** pendientes.
- **Facturas en otros idiomas y monedas**, y proveedores de otros países de la UE.

### Operación diaria y control de riesgo (v1.4)
- **Centro de alertas «Hoy»** en Inicio: lo que requiere acción, según el cargo y las obras de cada persona, con enlace a la pantalla donde se resuelve. La barra lateral muestra cuántos asuntos hay. Incluye:
  - facturas: críticas abiertas, pendientes de conformidad, retrasadas, sin leer;
  - pagos: vencidos, próximos 7 días, remesas sin confirmar;
  - cobros: vencidos, retenciones a reclamar, certificaciones sin facturar;
  - avales vencidos vivos, costes internos sin validar, compromisos vencidos, extras sin valorar y obras sin cerrar el mes anterior.
- **Cierre mensual con bloqueo del periodo**:
  - **Lista de comprobación**: certificación importada, facturas del mes resueltas, sin críticas, internos validados, casación, provisión y factura al cliente.
  - **Al cerrar**: se congela la foto del resultado (mes y origen) y el mes queda **bloqueado**. No se pueden editar, aprobar, releer ni añadir facturas ni costes internos de esa obra y mes.
  - **Reapertura**: solo Dirección o el administrador, con motivo, y queda registrada.
- **Pagos y remesas SEPA** (pain.001.001.03, admitido por los bancos españoles):
  - solo facturas aprobadas;
  - se **bloquean** las que no tienen IBAN, lo tienen no válido, tienen un **cambio de cuenta sin verificar** o una cuenta no verificada en un proveedor con varias;
  - una factura no puede ir en dos remesas;
  - las facturas se marcan pagadas **solo al confirmar** que el banco ejecutó la remesa, y una remesa no enviada se puede anular.
  - El ordenante (nombre e IBAN) se configura en Configuración.

## Novedades de la versión 2.0

Principio de siempre: **la máquina hace el trabajo repetitivo y prepara; la persona valida y aprueba.** Nada sale hacia un
proveedor, un cliente, el banco o SIS sin un circuito de aprobación configurable, y todo queda en la auditoría.

### Buzón de facturas (lectura automática del correo)
- Cada N minutos se leen los correos nuevos del buzón de facturas (IMAP; Gmail, Microsoft 365 con OAuth2 u otro servidor).
- Adjuntos: PDF, fotos y escaneos, ZIP (también anidados) y correos reenviados (.eml/.msg). Se descartan logotipos y firmas.
- **Solo entran las facturas**: cada PDF se clasifica por su contenido (dice «factura», base imponible, IVA, NIF de un tercero,
  total…) y se descartan presupuestos, ofertas, proformas, albaranes, certificaciones a cliente, nóminas, planos o certificados
  administrativos. Lo dudoso queda apartado para decidir con un clic, con el motivo de cada puntuación; y lo descartado se puede
  importar si el clasificador se equivocó.
- Las facturas entran igual que una subida manual (sin duplicados por huella) y se leen solas. El correo se marca como leído y,
  si se desea, se mueve a una carpeta de procesados. El mismo correo nunca se procesa dos veces.
- **Aprendizaje del remitente**: cuando se aprueba una factura llegada por correo, su remitente queda asociado al proveedor. Si la
  siguiente factura no trae el CIF legible (solo en el logotipo, escaneados), el proveedor se toma del remitente o del IBAN verificado.

### Copia de seguridad automática
- Diaria (y opcionalmente cada N horas) a uno o varios destinos: otro disco, NAS o carpeta de red, carpeta sincronizada.
- Base de datos: foto consistente aunque la app esté en uso, comprimida y **verificada** (integridad y recuentos) antes de darla por buena.
- Archivos (PDF, justificantes, planos, audios): espejo incremental que nunca borra.
- Rotación: últimas N diarias, semanales y mensuales. Aviso si la última copia correcta tiene más de un día, si falla un destino o
  si todas las copias están en el mismo disco que los datos. Simulacro de restauración desde la pantalla y `restaurar_copia.py`.
- La primera copia se hace nada más arrancar.

### Correo saliente y escritura en SIS
- **Bandeja de salida** con aprobación: reclamaciones de retenciones, peticiones de oferta con la separata adjunta, autorización de
  facturación a subcontratas, avisos de pago al confirmar una remesa, documentación CAE pendiente. Se decide qué tipos salen solos.
  Envío SMTP con reintentos; modo PRUEBA que genera .eml sin enviar.
- **Asientos en SIS por API**: cada factura aprobada se envía una sola vez (referencia e `Idempotency-Key`). Si la factura cambia
  después, queda «desfasada» para ajustarla en SIS. Modo PRUEBA, mapeo de campos configurable y el Excel/CSV de siempre como alternativa.
- Contraseñas cifradas en la instalación (o en variables de entorno `LLORCA_BUZON_CLAVE`, `LLORCA_SMTP_CLAVE`, `LLORCA_SIS_CLAVE`).

### Certificación a subcontratas y a cliente
- Líneas de contrato de cada oferta adjudicada (desde Excel, desde el estudio de ofertas o a mano) con la partida del cliente que les corresponde.
- **Certificación mensual al subcontratista**: la obra mide a origen o del mes; exceso sobre contrato solo con orden de cambio;
  aprobación a cuatro ojos; PDF «Autorización de facturación» (base, retención, IVA o ISP, líquido) y correo preparado pidiendo la
  factura por ese importe exacto.
- Su factura **casa sola** con la certificación aprobada (casación triple) y lo aprobado sin factura es coste devengado.
- **Preparar la certificación al cliente**: estructura y precios de la última certificación, avance de las subcontratas por
  partida, nunca por debajo de lo ya certificado, ajustes manuales guardados, margen del mes (venta frente a coste de subcontratas),
  Excel para SIS y comparación con la definitiva cuando se importa.

### Planificación de obra
- Actividades con duración en días laborables y dependencias FC/CC/FF con desfase; calendario con festivos y cierres; camino crítico.
- Línea base del planning aprobado y **replanificación** con lo real a la fecha de control: qué actividades se mueven por cada
  retraso, cuántos días, a quién avisar y cuánto se va el fin de obra. Gantt e importación desde Excel.

### Planos y fichas técnicas
- Revisiones con archivo y huella, estado (borrador, enviada a la DF, aprobada, con comentarios, rechazada, superada), quién de la
  DF aprobó y justificante. «Vigente» = última aprobada; aviso si hay una posterior sin aprobar y de las copias entregadas de
  revisiones superadas que hay que retirar de obra.

### Seguridad y Salud
- CAE: empresas por obra con alta, baja y nivel de subcontratación (máximo configurable, Ley 32/2006); trabajadores; documentación
  exigida con caducidad y validación; correo a la empresa con lo que le falta.
- Control de acceso por DNI/NIE con registro de entradas permitidas y denegadas y los motivos.
- Opcional: aviso o bloqueo del pago a subcontratas sin certificado de estar al corriente con la AEAT (art. 43.1.f LGT).
- Residuos: retiradas por código LER con albarán, factura (enlazada sola por el nº de albarán) y certificado del gestor;
  trazabilidad completa o qué falta; comparación con el estudio de gestión de residuos.

### Conciliación bancaria
- Extractos Norma 43 (verificados con sus propios totales) o Excel/CSV del banco; sin duplicados aunque se solapen.
- Un cargo igual a una remesa **confirma la remesa** y marca pagadas sus facturas (antes se hacía a mano); también pagos sueltos y
  pagos detallados de una remesa. Los abonos registran el cobro de la factura emitida o de su retención.
- Lo inequívoco se aplica solo; lo demás se propone con su confianza. Todo se puede deshacer.

### Actas desde el audio de la reunión
- Transcripción local con Whisper (el audio no sale de la oficina), resumen opcional con la IA local, propuesta de compromisos,
  decisiones y extras con responsable, fecha e importe, y paso al diario de obra tras revisarlo.

### Técnicas
- **HTTPS**: `python generar_certificado.py` crea el certificado y `servidor.bat` arranca en https:// (cookie de sesión «Secure»).
  Para internet: detrás de un proxy inverso con certificado público, sin abrir el puerto 8501.
- **Pruebas automáticas** con pytest en cada cambio (ver «Arquitectura»). Han encontrado y corregido un fallo real: dos remesas
  generadas en el mismo segundo chocaban por la referencia.
- Compatibilidad con Python 3.11 además de 3.12.
- Centro de alertas ampliado: copias, tareas automáticas con error, buzón, correos por aprobar o fallidos, asientos desfasados en SIS,
  movimientos bancarios sin conciliar, certificaciones de subcontrata por aprobar, planning retrasado, planos sin respuesta de la DF,
  CAE caducada y residuos sin certificado.
