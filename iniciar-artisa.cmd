@echo off
setlocal

rem Inicio manual de Artisa desde la terminal PowerShell o CMD de Visual Studio Code.
set "ARTISA_WINDOWS_PATH=%~dp0Capstone Artisa"
set "ARTISA_WSL_PATH="

for /f "usebackq delims=" %%P in (`wsl.exe -d Ubuntu --exec wslpath -a "%ARTISA_WINDOWS_PATH%"`) do set "ARTISA_WSL_PATH=%%P"

if not defined ARTISA_WSL_PATH (
  echo No se pudo convertir la ruta del proyecto para WSL.
  exit /b 1
)

wsl.exe -d Ubuntu --cd "%ARTISA_WSL_PATH%" python3 scripts/artisa.py iniciar
exit /b %ERRORLEVEL%
