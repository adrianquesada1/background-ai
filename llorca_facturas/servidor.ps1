. "$PSScriptRoot\_comun.ps1"

Write-Host ""
Write-Host "  LLORCA GROUP - Control economico de obra (MODO SERVIDOR)" -ForegroundColor White
$conda = Buscar-Conda
if (-not $conda) { Fallo "No encuentro Anaconda. Ejecuta primero INSTALAR." }
$ruta = Ruta-Entorno $conda
if (-not $ruta) { Fallo "El entorno '$EnvName' no existe. Ejecuta primero INSTALAR." }
Activar-Entorno $ruta
OK "Entorno Python listo"

Preparar-Entorno-Ollama
$ollama = Buscar-Ollama
if ($ollama) { [void](Asegurar-Ollama $ollama) }
else { Aviso "Ollama no instalado: se leera con OCR + reglas (sin IA local)" }

# Regla del cortafuegos para el puerto 8501 (solo red privada/dominio). Requiere permisos: si falla, se avisa.
try {
    if (-not (Get-NetFirewallRule -DisplayName "Llorca Control Obra 8501" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "Llorca Control Obra 8501" -Direction Inbound -Protocol TCP -LocalPort 8501 -Action Allow -Profile Private,Domain | Out-Null
        OK "Cortafuegos: puerto 8501 abierto en la red privada"
    } else { OK "Cortafuegos: puerto 8501 ya abierto" }
} catch { Aviso "No se pudo abrir el puerto 8501 en el cortafuegos. Ejecuta SERVIDOR como administrador una vez." }

$ips = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254*" }).IPAddress

# HTTPS: si existe el certificado (python generar_certificado.py), la app se sirve cifrada
$crt = Join-Path $PSScriptRoot "certificados\servidor.crt"
$key = Join-Path $PSScriptRoot "certificados\servidor.key"
$extra = @()
$proto = "http"
if ((Test-Path $crt) -and (Test-Path $key)) {
    $extra = @("--server.sslCertFile", $crt, "--server.sslKeyFile", $key)
    $proto = "https"
    OK "HTTPS activado con certificados\servidor.crt"
} else {
    Aviso "Sin HTTPS (solo red interna). Para cifrar: python generar_certificado.py"
}
Write-Host ""
Write-Host "  Acceso desde cualquier equipo de la oficina:" -ForegroundColor Cyan
foreach ($i in $ips) { Write-Host "     $($proto)://$($i):8501" -ForegroundColor Green }
Write-Host "     $($proto)://$($env:COMPUTERNAME):8501" -ForegroundColor Green
Write-Host "  Deja esta ventana abierta. Para apagar el servidor: Ctrl + C" -ForegroundColor Yellow
Write-Host "  Las tareas automaticas (buzon, copias, correo, SIS) solo funcionan mientras el servidor esta en marcha." -ForegroundColor Yellow
Write-Host ""
& "$ruta\python.exe" -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true @extra
Read-Host "El servidor se ha detenido. Pulsa Enter para salir"
