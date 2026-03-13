@echo off
setlocal
cd /d "%~dp0"
cls
echo ================================================================
echo                    STOP BRIDGE-U-F WORKBENCH
echo ================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench_ctl.ps1" -Action stop
set "WB_EXIT=%ERRORLEVEL%"

echo.
if "%WB_EXIT%"=="0" (
    echo Workbench stop: completed
) else (
    echo Workbench stop: failed
)
echo.
pause
exit /b %WB_EXIT%
