@echo off
chcp 65001 >nul
setlocal

:: ============================================================
:: HASHI - Start TUI
:: Auto-starts main bridge in a new window if not already running.
::
:: The TUI is the rescue hatch. It only needs textual / rich / aiohttp,
:: so this script never blocks on the full Core/Functions dependency
:: contract. Interpreters are tried in this order:
::   1. installer-qualified Functions runtime
::   2. portable private runtime  \python\python.exe
::   3. developer venv            \.venv\Scripts\python.exe
::   4. a Python 3 on PATH
:: Server dependency drift is reported inside the TUI (/doctor), never by
:: refusing to start it.
:: Logs written to: windows\logs\tui_<timestamp>.log
:: ============================================================

set ROOT=%~dp0..
set LOG_DIR=%~dp0logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=1-3 delims=/ " %%a in ("%DATE%") do set D=%%a-%%b-%%c
for /f "tokens=1-3 delims=:." %%a in ("%TIME: =0%") do set T=%%a%%b%%c
set LOG_FILE=%LOG_DIR%\tui_%D%_%T%.log

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
echo HASHI TUI >> "%LOG_FILE%"
echo Started: %DATE% %TIME% >> "%LOG_FILE%"
echo Python: %PYTHON_EXE% >> "%LOG_FILE%"
echo ==================================================== >> "%LOG_FILE%"

:: ---- Server runtime: reported by /doctor, never by this launcher ----
:: The TUI does not use Playwright, greenlet, pyee or any other server-side
:: package, so running the server dependency contract here would check the
:: wrong role under the TUI interpreter and blame the TUI for a server
:: mismatch. /doctor reports the Core/Functions pair from the qualified
:: selector instead.

set "PID_FILE="
set "INSTANCE_ID=HASHI"
for /f "delims=" %%P in ('""%PYTHON_EXE%" -B "%ROOT%\scripts\resolve_instance_runtime.py" --code-root "%ROOT%" --bridge-home "%ROOT%" --field pid-path" 2^>nul') do set "PID_FILE=%%P"
for /f "delims=" %%P in ('""%PYTHON_EXE%" -B "%ROOT%\scripts\resolve_instance_runtime.py" --code-root "%ROOT%" --bridge-home "%ROOT%" --field instance-id" 2^>nul') do set "INSTANCE_ID=%%P"

:: Check if main bridge is running via PID file
set BRIDGE_RUNNING=0
if defined PID_FILE if exist "%PID_FILE%" (
    for /f "usebackq delims=" %%P in ("%PID_FILE%") do (
        powershell -NoProfile -Command "if (Get-Process -Id %%P -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
        if not errorlevel 1 set BRIDGE_RUNNING=1
    )
)

:: Auto-start bridge in a new window if not running
if "%BRIDGE_RUNNING%"=="0" (
    echo Bridge not running - starting it in a new window...
    echo Bridge not running - starting it in a new window... >> "%LOG_FILE%"
    start "%INSTANCE_ID% Main Bridge" /D "%ROOT%" cmd /c "bin\bridge-u.bat --resume-last"
    echo Waiting 15 seconds for bridge to initialize...
    timeout /t 15 /nobreak >nul
)

echo Starting %INSTANCE_ID% TUI...
echo Log file: %LOG_FILE%
cd /d "%ROOT%"
set PYTHONPATH=%ROOT%
"%PYTHON_EXE%" -B tui.py 2>> "%LOG_FILE%"
set EXIT_CODE=%ERRORLEVEL%

echo. >> "%LOG_FILE%"
echo Exit code: %EXIT_CODE% >> "%LOG_FILE%"
echo Stopped: %DATE% %TIME% >> "%LOG_FILE%"

echo.
echo ============================================================
echo TUI stopped. Exit code: %EXIT_CODE%
echo Log saved to: %LOG_FILE%
echo ============================================================
pause
endlocal
