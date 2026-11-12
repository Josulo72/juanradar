@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "motor\.venv\Scripts\pythonw.exe" (
  echo.
  echo  Falta instalar. Abri primero "1-INSTALAR".
  echo.
  pause
  exit /b 1
)
rem Abre el panel en el navegador y deja el robot trabajando en segundo plano.
start "" "motor\.venv\Scripts\pythonw.exe" "%~dp0radar.py"
