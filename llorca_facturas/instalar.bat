@echo off
REM Doble clic: ejecuta instalar.ps1 sin cambiar la politica del equipo
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1"
if errorlevel 1 pause
