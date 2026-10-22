@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0detener-artisa.ps1"
exit /b %ERRORLEVEL%
