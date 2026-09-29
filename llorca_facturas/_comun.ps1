# ============================================================
#  LLORCA GROUP - funciones comunes de INSTALAR / INICIAR / SERVIDOR
#  (se carga con:  . "$PSScriptRoot\_comun.ps1")
# ============================================================
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$EnvName         = "llorca_facturas"
$ModeloVision    = "qwen3-vl:4b-instruct"
$ModeloRazon     = "qwen3:4b-instruct"
$ModeloEmbedding = "qwen3-embedding:0.6b"
$Modelos         = Join-Path $PSScriptRoot "modelos"
$OllamaUrl       = "http://127.0.0.1:11434"      # IPv4 fijo: 'localhost' puede ir por IPv6 o por el proxy
$OllamaLog       = Join-Path $PSScriptRoot "ollama_serve.log"

# Nada de proxy para las llamadas locales (PowerShell y Python/requests)
[System.Net.WebRequest]::DefaultWebProxy = $null
$env:NO_PROXY = "127.0.0.1,localhost"
$env:no_proxy = $env:NO_PROXY

function Titulo($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function OK($t)     { Write-Host "  [OK] $t" -ForegroundColor Green }
function Aviso($t)  { Write-Host "  [!]  $t" -ForegroundColor Yellow }
function Fallo($t)  { Write-Host "  [X]  $t" -ForegroundColor Red; Read-Host "Pulsa Enter para salir"; exit 1 }

# Localiza conda.exe aunque no este en el PATH
function Buscar-Conda {
    $cmd = Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $rutas = @("$env:USERPROFILE\anaconda3", "$env:USERPROFILE\miniconda3", "$env:LOCALAPPDATA\anaconda3",
               "$env:LOCALAPPDATA\miniconda3", "C:\ProgramData\anaconda3", "C:\ProgramData\miniconda3",
               "C:\anaconda3", "C:\miniconda3", "D:\anaconda3", "D:\miniconda3")
    foreach ($r in $rutas) {
        $exe = Join-Path $r "Scripts\conda.exe"
        if (Test-Path $exe) { return $exe }
    }
    return $null
}

# Ruta del entorno (null si no existe)
function Ruta-Entorno($conda) {
    $json = & $conda env list --json | Out-String | ConvertFrom-Json
    foreach ($e in $json.envs) { if ((Split-Path $e -Leaf) -eq $EnvName) { return $e } }
    return $null
}

# Equivale a "conda activate": pone en el PATH las carpetas del entorno (DLLs incluidas)
function Activar-Entorno($ruta) {
    $env:PATH = "$ruta;$ruta\Library\mingw-w64\bin;$ruta\Library\usr\bin;$ruta\Library\bin;$ruta\Scripts;$env:PATH"
    $env:CONDA_PREFIX = $ruta
}

function Buscar-Ollama {
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($r in @("$env:LOCALAPPDATA\Programs\Ollama\ollama.exe", "D:\Ollama\ollama.exe", "C:\Program Files\Ollama\ollama.exe")) {
        if (Test-Path $r) { return $r }
    }
    return $null
}

function Ollama-Activo {
    try { Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
}

# PID del proceso que tiene ocupado el puerto 11434 (null si esta libre)
function Pid-Puerto-Ollama {
    try {
        $c = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction Stop | Select-Object -First 1
        return $c.OwningProcess
    } catch { return $null }
}

# Variables de rendimiento + carpeta de modelos del usuario
function Preparar-Entorno-Ollama {
    $env:OLLAMA_NUM_PARALLEL = "1"
    $env:OLLAMA_MAX_LOADED_MODELS = "1"
    $env:OLLAMA_KEEP_ALIVE = "5m"
    $m = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "User")
    if ($m) { $env:OLLAMA_MODELS = $m }
}

# Deja Ollama respondiendo. Devuelve $true/$false. Nunca espera en silencio: si falla, ensena el motivo.
function Asegurar-Ollama($ollama, [int]$espera = 45) {
    if (Ollama-Activo) { OK "IA local (Ollama) en marcha ($OllamaUrl)"; return $true }

    $pidOcupa = Pid-Puerto-Ollama
    if ($pidOcupa) {
        $nombre = (Get-Process -Id $pidOcupa -ErrorAction SilentlyContinue).ProcessName
        Aviso "El puerto 11434 lo tiene '$nombre' (PID $pidOcupa) pero no responde. Esperando..."
        foreach ($i in 1..15) { Start-Sleep -Seconds 1; if (Ollama-Activo) { OK "IA local (Ollama) en marcha"; return $true } }
        Aviso "Sigue sin responder. Cierra Ollama (icono de la llama > Quit) o ejecuta:  taskkill /f /im ollama.exe"
        return $false
    }

    Write-Host "  Arrancando Ollama..." -ForegroundColor Gray
    try {
        $p = Start-Process -FilePath $ollama -ArgumentList "serve" -NoNewWindow -PassThru `
             -RedirectStandardError $OllamaLog -RedirectStandardOutput "$OllamaLog.out"
    } catch {
        Aviso "No se pudo lanzar '$ollama serve': $($_.Exception.Message)"
        return $false
    }
    foreach ($i in 1..$espera) {
        Start-Sleep -Seconds 1
        if (Ollama-Activo) { OK "IA local (Ollama) arrancada ($OllamaUrl)"; return $true }
        if ($p.HasExited) { break }
    }
    Aviso "Ollama no responde. La app funcionara con OCR + reglas y podras reintentar desde Configuracion."
    Write-Host "       Ultimas lineas de ollama_serve.log:" -ForegroundColor DarkYellow
    Get-Content $OllamaLog -Tail 12 -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "       $_" -ForegroundColor DarkYellow }
    return $false
}
