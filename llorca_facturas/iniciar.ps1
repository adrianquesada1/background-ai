. "$PSScriptRoot\_comun.ps1"

Write-Host ""
Write-Host "  LLORCA GROUP - Control economico de obra" -ForegroundColor White

$conda = Buscar-Conda
if (-not $conda) { Fallo "No encuentro Anaconda. Ejecuta primero INSTALAR." }
$ruta = Ruta-Entorno $conda
if (-not $ruta) { Fallo "El entorno '$EnvName' no existe. Ejecuta primero INSTALAR." }
Activar-Entorno $ruta
OK "Entorno Python listo"

# IA local: usa el Ollama que ya este abierto o lo arranca
Preparar-Entorno-Ollama
$ollama = Buscar-Ollama
if ($ollama) { [void](Asegurar-Ollama $ollama) }
else { Aviso "Ollama no instalado: se leera con OCR + reglas (sin IA local)" }

Write-Host ""
Write-Host "  Abriendo la app en el navegador (http://localhost:8501)..." -ForegroundColor Cyan
Write-Host "  NO cierres esta ventana mientras uses la app. Para salir: Ctrl + C" -ForegroundColor Yellow
Write-Host ""
& "$ruta\python.exe" -m streamlit run app.py --server.port 8501
Read-Host "La app se ha cerrado. Pulsa Enter para salir"
