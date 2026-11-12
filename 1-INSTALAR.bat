@echo off
chcp 65001 >nul
title Radar Autonomos - Instalacion
cd /d "%~dp0"
echo.
echo  RADAR AUTONOMOS - instalacion (una sola vez, tarda entre 5 y 15 minutos)
echo  Necesita internet. No cierres esta ventana hasta que diga LISTO.
echo.

set "PY="
py -3.12 --version >nul 2>nul && set "PY=py -3.12"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
  echo  No encuentro Python en esta computadora.
  echo  Instala Python 3.12 desde https://www.python.org/downloads/  marcando la casilla
  echo  "Add python.exe to PATH", y despues volve a abrir este archivo.
  echo.
  pause
  exit /b 1
)

echo  [1/3] Preparando el motor de busqueda...
%PY% -m venv "motor\.venv"
if errorlevel 1 goto :fallo

echo  [2/3] Descargando componentes (la parte mas larga)...
"motor\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
"motor\.venv\Scripts\python.exe" -m pip install -r "motor\requirements.txt"
if errorlevel 1 goto :fallo

echo  [3/3] Descargando el navegador interno del motor...
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0motor\browsers"
"motor\.venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto :fallo

echo.
echo  LISTO. Ya podes abrir "2-ABRIR-RADAR".
echo.
pause
exit /b 0

:fallo
echo.
echo  Algo fallo durante la instalacion. Revisa la conexion a internet y volve a abrir este archivo.
echo  Si se repite, copia el texto de esta ventana para pedir ayuda.
echo.
pause
exit /b 1
