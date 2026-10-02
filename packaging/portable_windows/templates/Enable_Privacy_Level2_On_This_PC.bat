@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Enable-Privacy-Level2.ps1"
exit /b %ERRORLEVEL%
