@echo off
REM Servidor para toda la red de la oficina (la primera vez: clic derecho > Ejecutar como administrador)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0servidor.ps1"
if errorlevel 1 pause
