@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Bootstrap-Elevated.ps1" -Action Update -Surface TUI
exit /b %ERRORLEVEL%
