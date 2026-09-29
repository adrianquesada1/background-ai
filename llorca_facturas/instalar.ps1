. "$PSScriptRoot\_comun.ps1"

Write-Host ""
Write-Host "  LLORCA GROUP - Control economico de obra" -ForegroundColor White
Write-Host "  Instalacion / actualizacion" -ForegroundColor Gray

Titulo "1/4  Anaconda"
$conda = Buscar-Conda
if (-not $conda) { Fallo "No encuentro Anaconda. Instala Anaconda (Anaconda3-...-Windows-x86_64.exe) y vuelve a ejecutar INSTALAR." }
OK "Anaconda: $conda"

Titulo "2/4  Entorno Python '$EnvName'"
$env:CONDA_ALWAYS_YES = "true"
$ruta = Ruta-Entorno $conda
if ($ruta) {
    Write-Host "  El entorno ya existe: actualizando paquetes (puede tardar)..."
    & $conda env update -n $EnvName -f environment.yml --prune
} else {
    Write-Host "  Creando el entorno (la primera vez tarda varios minutos)..."
    & $conda env create -f environment.yml
}
if ($LASTEXITCODE -ne 0) { Fallo "Conda ha dado un error. Haz una captura de esta ventana." }
$ruta = Ruta-Entorno $conda
if (-not $ruta) { Fallo "El entorno no se ha creado." }
OK "Entorno en $ruta"

Titulo "3/4  Comprobando librerias"
Activar-Entorno $ruta
& "$ruta\python.exe" -c "import streamlit, pandas, plotly, openpyxl, rapidfuzz, pdfplumber, pypdfium2, rapidocr_onnxruntime, requests, chromadb; print('  todas las librerias cargan bien')"
if ($LASTEXITCODE -ne 0) { Fallo "Alguna libreria no carga. Haz una captura de esta ventana." }
OK "Librerias correctas (incluido el OCR local)"

Titulo "4/4  IA local (Ollama) - opcional"
# Carpeta de modelos: se respeta la que ya exista; si apunta a una carpeta que ya no existe, se usa la de este proyecto
$actual = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "User")
if ($actual -and (Test-Path $actual)) {
    $env:OLLAMA_MODELS = $actual
    OK "Carpeta de modelos: $actual"
} else {
    if ($actual) { Aviso "OLLAMA_MODELS apuntaba a '$actual', que ya no existe." }
    New-Item -ItemType Directory -Force -Path $Modelos | Out-Null
    [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $Modelos, "User")
    $env:OLLAMA_MODELS = $Modelos
    OK "Los modelos se guardaran en $Modelos"
    Aviso "Si Ollama ya estaba abierto, cierralo (icono de la llama > Quit) para que use la carpeta nueva."
}
Preparar-Entorno-Ollama

$ollama = Buscar-Ollama
if (-not $ollama) {
    Aviso "Ollama no esta instalado. La app funciona igual (OCR + reglas). Para la IA local instala Ollama y vuelve a ejecutar INSTALAR."
} elseif (Asegurar-Ollama $ollama) {
    $env:OLLAMA_HOST = $OllamaUrl
    $lista = & $ollama list | Out-String
    foreach ($m in @($ModeloVision, $ModeloRazon, $ModeloEmbedding)) {
        if ($lista -match [regex]::Escape($m)) {
            OK "Modelo $m instalado"
        } else {
            $r = Read-Host "  Falta $m. Descargarlo ahora? (s/n)"
            if ($r -match "^[sS]") { & $ollama pull $m } else { Aviso "Puedes descargarlo luego con: ollama pull $m" }
        }
    }
    Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue
} else {
    Aviso "No se han podido comprobar los modelos. Revisa el log de arriba y vuelve a ejecutar INSTALAR."
}

Write-Host ""
Write-Host "  TODO LISTO. Para abrir la app: doble clic en INICIAR" -ForegroundColor Green
Read-Host "Pulsa Enter para cerrar"
