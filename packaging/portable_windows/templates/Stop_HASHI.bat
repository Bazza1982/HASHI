@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Stop-HASHI.ps1"
if errorlevel 1 pause
