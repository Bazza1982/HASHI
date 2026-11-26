@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem HASHI - TUI Onboarding
rem First-run setup: language, disclaimer, API key check,
rem then seamless chat with Hashiko for Telegram + agent setup.
rem
rem The onboarding UI is part of the rescue hatch: it needs only
rem textual / rich / aiohttp and never blocks on the full server
rem dependency contract. Interpreters are tried in this order:
rem   1. installer-qualified Functions runtime
rem   2. portable private runtime  \python\python.exe
rem   3. developer venv            \.venv\Scripts\python.exe
rem   4. a Python 3 on PATH
rem ============================================================

set ROOT=%~dp0..
set LOG_DIR=%~dp0logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=1-3 delims=/ " %%a in ("%DATE%") do set D=%%a-%%b-%%c
for /f "tokens=1-3 delims=:." %%a in ("%TIME: =0%") do set T=%%a%%b%%c
set LOG_FILE=%LOG_DIR%\tui_onboarding_%D%_%T%.log

:: ---- Bootstrap interpreter (may lack the TUI packages; stdlib only) ----
set "BOOTSTRAP="
if exist "%ROOT%\python\python.exe" set "BOOTSTRAP=%ROOT%\python\python.exe"
if not defined BOOTSTRAP if exist "%ROOT%\.venv\Scripts\python.exe" set "BOOTSTRAP=%ROOT%\.venv\Scripts\python.exe"
if not defined BOOTSTRAP set "BOOTSTRAP=python"

:: ---- TUI interpreter: first candidate that can import textual/rich/aiohttp ----
set "PYTHON_EXE="
set "TUI_PY_FILE=%TEMP%\hashi_tui_python_%RANDOM%.txt"
"%BOOTSTRAP%" -B "%ROOT%\scripts\resolve_tui_python.py" --code-root "%ROOT%" > "%TUI_PY_FILE%" 2>nul
if exist "%TUI_PY_FILE%" set /p PYTHON_EXE=<"%TUI_PY_FILE%"
if exist "%TUI_PY_FILE%" del "%TUI_PY_FILE%" >nul 2>&1
if not defined PYTHON_EXE set "PYTHON_EXE=%BOOTSTRAP%"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%BOOTSTRAP%"

echo ==================================================== >> "%LOG_FILE%"
echo HASHI TUI Onboarding >> "%LOG_FILE%"
echo Started: %DATE% %TIME% >> "%LOG_FILE%"
echo Python: %PYTHON_EXE% >> "%LOG_FILE%"
echo ==================================================== >> "%LOG_FILE%"

:: ---- Server runtime contract: informational only, never fatal ----
set "HASHI_TUI_RUNTIME_STATUS=unknown"
"%PYTHON_EXE%" -B "%ROOT%\scripts\check_runtime_contract.py" --code-root "%ROOT%" >nul 2>> "%LOG_FILE%"
if errorlevel 1 (
    set "HASHI_TUI_RUNTIME_STATUS=drifted"
    echo NOTICE: the Core/Functions server dependency contract is not satisfied.>> "%LOG_FILE%"
    echo         Onboarding still starts. Open /doctor for the exact list.
) else (
    set "HASHI_TUI_RUNTIME_STATUS=ok"
)

set "PID_FILE="
set "INSTANCE_ID=HASHI"
for /f "delims=" %%P in ('"%PYTHON_EXE%" -B "%ROOT%\scripts\resolve_instance_runtime.py" --code-root "%ROOT%" --bridge-home "%ROOT%" --field pid-path') do set "PID_FILE=%%P"
for /f "delims=" %%P in ('"%PYTHON_EXE%" -B "%ROOT%\scripts\resolve_instance_runtime.py" --code-root "%ROOT%" --bridge-home "%ROOT%" --field instance-id') do set "INSTANCE_ID=%%P"

rem Check if main bridge is running via PID file
set BRIDGE_RUNNING=0
if defined PID_FILE if exist "%PID_FILE%" (
    for /f "usebackq delims=" %%P in ("%PID_FILE%") do (
        powershell -NoProfile -Command "if (Get-Process -Id %%P -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
        if not errorlevel 1 set BRIDGE_RUNNING=1
    )
)

rem Auto-start bridge in a new window if not running
if "%BRIDGE_RUNNING%"=="0" (
    echo Bridge not running - starting it in a new window...
    echo Bridge not running - starting it in a new window... >> "%LOG_FILE%"
    start "%INSTANCE_ID% Main Bridge" /D "%ROOT%" cmd /c "bin\bridge-u.bat --resume-last"
    echo Waiting 15 seconds for bridge to initialize...
    timeout /t 15 /nobreak >nul
)

echo Starting %INSTANCE_ID% TUI Onboarding...
echo Log file: %LOG_FILE%
if "%HASHI_TUI_RUNTIME_STATUS%"=="drifted" (
    echo NOTE: Core/Functions dependencies need attention; onboarding starts anyway.
    echo.
)

cd /d "%ROOT%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%ROOT%
"%PYTHON_EXE%" -B tui_onboarding.py 2>> "%LOG_FILE%"
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
