@echo off
setlocal
cd /d "%~dp0"

set "QUIET_MODE=0"
if /i "%~1"=="--quiet" set "QUIET_MODE=1"

if "%QUIET_MODE%"=="0" (
    cls
    echo ================================================================
    echo                     BRIDGE-U-F WORKBENCH
    echo ================================================================
    echo.
    echo   Mode : start supervised workbench services
    echo   Path : %~dp0workbench
    echo.
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench_ctl.ps1" -Action start -OpenBrowser
set "WB_EXIT=%ERRORLEVEL%"

if "%QUIET_MODE%"=="0" (
    echo.
    if "%WB_EXIT%"=="0" (
        echo Workbench start: healthy
    ) else (
        echo Workbench start: failed
    )
    echo Logs: %~dp0state\workbench\logs
    echo.
    if not "%WB_EXIT%"=="0" pause
)

exit /b %WB_EXIT%
