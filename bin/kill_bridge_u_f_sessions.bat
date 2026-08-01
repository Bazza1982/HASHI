@echo off
setlocal EnableDelayedExpansion

set "QUIET=0"
set "NO_PAUSE=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--quiet" set "QUIET=1"
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"
shift
goto parse_args

:args_done
set "CONTROL=%~dp0bridge_ctl.ps1"
if not exist "!CONTROL!" (
    if "!QUIET!"=="0" echo ERROR: bridge_ctl.ps1 is required for instance-scoped shutdown.
    exit /b 1
)

if "!QUIET!"=="0" (
    echo ================================================================
    echo                  STOP THIS HASHI INSTANCE
    echo ================================================================
    echo.
)

if "!QUIET!"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "!CONTROL!" -Action kill -Quiet
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "!CONTROL!" -Action kill
)
set "CONTROL_EXIT=!ERRORLEVEL!"

if not "!CONTROL_EXIT!"=="0" if "!QUIET!"=="0" (
    echo.
    echo Shutdown was incomplete. No other HASHI instance was scanned or stopped.
)
if "!NO_PAUSE!"=="0" if "!QUIET!"=="0" pause
exit /b !CONTROL_EXIT!
