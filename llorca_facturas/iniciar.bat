@echo off
REM Doble clic: ejecuta iniciar.ps1 sin cambiar la politica del equipo
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar.ps1"
if errorlevel 1 pause
