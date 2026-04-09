@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem HASHI9 - TUI Onboarding
rem First-run setup: language, disclaimer, API key check,
rem then seamless chat with Hashiko for Telegram + agent setup.
rem USB mode: uses embedded Python from \python\ if present.
rem Fallback: uses .venv Python for local dev installs.
rem ============================================================

set ROOT=%~dp0..
set PYTHON_EXE=%ROOT%\python\python.exe
if not exist "%PYTHON_EXE%" set PYTHON_EXE=%ROOT%\.venv\Scripts\python.exe
if not exist "%PYTHON_EXE%" set PYTHON_EXE=python
set PID_FILE=%ROOT%\.bridge_u_f.pid
set LOG_DIR=%~dp0logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=1-3 delims=/ " %%a in ("%DATE%") do set D=%%a-%%b-%%c
for /f "tokens=1-3 delims=:." %%a in ("%TIME: =0%") do set T=%%a%%b%%c
set LOG_FILE=%LOG_DIR%\tui_onboarding_%D%_%T%.log

echo ==================================================== >> "%LOG_FILE%"
echo HASHI9 TUI Onboarding >> "%LOG_FILE%"
echo Started: %DATE% %TIME% >> "%LOG_FILE%"
echo Python: %PYTHON_EXE% >> "%LOG_FILE%"
echo ==================================================== >> "%LOG_FILE%"

if not exist "%PYTHON_EXE%" (
    echo ERROR: Python not found. >> "%LOG_FILE%"
    echo.
    echo ERROR: Python not found at %PYTHON_EXE%
    echo        On USB: run prepare_usb.bat to set up embedded Python.
    echo        On dev machine: run start_main.bat first to create .venv.
    pause
    exit /b 1
)

rem Check if main bridge is running via PID file
set BRIDGE_RUNNING=0
if exist "%PID_FILE%" (
    for /f "usebackq delims=" %%P in ("%PID_FILE%") do (
        powershell -NoProfile -Command "if (Get-Process -Id %%P -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
        if not errorlevel 1 set BRIDGE_RUNNING=1
    )
)

rem Auto-start bridge in a new window if not running
if "%BRIDGE_RUNNING%"=="0" (
    echo Bridge not running - starting it in a new window...
    echo Bridge not running - starting it in a new window... >> "%LOG_FILE%"
    start "HASHI9 Main Bridge" /D "%ROOT%" cmd /c "bin\bridge-u.bat --resume-last"
    echo Waiting 15 seconds for bridge to initialize...
    timeout /t 15 /nobreak >nul
)

echo Starting HASHI9 TUI Onboarding...
echo Log file: %LOG_FILE%
echo.

cd /d "%ROOT%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%ROOT%
"%PYTHON_EXE%" tui_onboarding.py 2>> "%LOG_FILE%"
set EXIT_CODE=%ERRORLEVEL%

echo. >> "%LOG_FILE%"
echo Exit code: %EXIT_CODE% >> "%LOG_FILE%"
echo Stopped: %DATE% %TIME% >> "%LOG_FILE%"

if %EXIT_CODE% NEQ 0 (
    echo.
    echo [ERROR] TUI Onboarding exited with error code %EXIT_CODE%.
    echo Log saved to: %LOG_FILE%
    pause
)
endlocal
