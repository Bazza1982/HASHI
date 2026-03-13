@echo off
setlocal
cd /d "%~dp0"
cls
echo ================================================================
echo                  RESTART BRIDGE-U-F WORKBENCH
echo ================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench_ctl.ps1" -Action restart -OpenBrowser
set "WB_EXIT=%ERRORLEVEL%"

echo.
if "%WB_EXIT%"=="0" (
    echo Workbench restart: healthy
) else (
    echo Workbench restart: failed
)
echo Logs: %~dp0state\workbench\logs
echo.
if not "%WB_EXIT%"=="0" pause
exit /b %WB_EXIT%
