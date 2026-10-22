@echo off
setlocal

rem Inicio manual desde la terminal integrada de Visual Studio Code.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar-artisa.ps1"
exit /b %ERRORLEVEL%
