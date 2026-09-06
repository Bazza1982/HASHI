@echo off
chcp 65001 >nul
setlocal
start "" powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0launcher\Bootstrap-Elevated.ps1" -Action Diagnose
exit /b 0
