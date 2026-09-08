@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Bootstrap-Elevated.ps1" -Action Rollback -Surface TUI
exit /b %ERRORLEVEL%
