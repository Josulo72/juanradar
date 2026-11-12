@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Opcional: hace que el Radar se encienda solo cada vez que prendes la computadora
rem (sin abrir el navegador). Para deshacerlo, borra el acceso directo "Radar Autonomos"
rem de la carpeta que se abre con  Win+R  ->  shell:startup
if not exist "motor\.venv\Scripts\pythonw.exe" (
  echo  Falta instalar. Abri primero "1-INSTALAR".
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = [Environment]::GetFolderPath('Startup'); $s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Radar Autonomos.lnk')); $s.TargetPath = (Join-Path '%~dp0' 'motor\.venv\Scripts\pythonw.exe'); $s.Arguments = '\"' + (Join-Path '%~dp0' 'radar.py') + '\" --sin-navegador'; $s.WorkingDirectory = '%~dp0'; $s.Save()"
if errorlevel 1 (
  echo  No pude crear el acceso directo.
) else (
  echo  Hecho. El Radar se va a encender solo al prender la computadora.
  echo  Para ver el panel, abri "2-ABRIR-RADAR".
)
echo.
pause
