@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
title Bridge-U-F Launcher

rem This script lives under <repo>\bin\. We want BRIDGE_CODE_ROOT to be the repo root,
rem not the bin folder, otherwise agents.json will be looked up in the wrong place.
set "SCRIPT_DIR=%~dp0"
if "!SCRIPT_DIR:~-1!"=="\" set "SCRIPT_DIR=!SCRIPT_DIR:~0,-1!"

set "BRIDGE_CODE_ROOT=!SCRIPT_DIR!\.."
for %%I in ("!BRIDGE_CODE_ROOT!") do set "BRIDGE_CODE_ROOT=%%~fI"
cd /d "!BRIDGE_CODE_ROOT!"

:: USB mode: use embedded Python if present (no system Python or venv required)
set "PYTHON_EXE=python"
set "USING_EMBEDDED=0"
if exist "!BRIDGE_CODE_ROOT!\python\python.exe" (
    set "PYTHON_EXE=!BRIDGE_CODE_ROOT!\python\python.exe"
    set "USING_EMBEDDED=1"
)

if not defined BRIDGE_HOME set "BRIDGE_HOME=!BRIDGE_CODE_ROOT!"
if "!BRIDGE_HOME:~-1!"=="\" set "BRIDGE_HOME=!BRIDGE_HOME:~0,-1!"

set "STATE_FILE=%BRIDGE_HOME%\.bridge_u_last_agents.txt"
set "AGENTS_FILE=%TEMP%\bridge_u_active_agents.txt"
set "INACTIVE_FILE=%TEMP%\bridge_u_inactive_agents.txt"
set "WORKBENCH_LAUNCH=0"
set "API_GATEWAY_LAUNCH=0"
set "AUTO_STOP_WORKBENCH=0"
set "AUTO_RESUME_LAST=0"
set "NO_PAUSE=0"
set "DRY_RUN=0"
set "WAKEUP_FILE="

call :parse_args %*

call :init_theme
call :ensure_env || exit /b 1
call :load_agents || exit /b 1
call :load_last_state

if "!AUTO_RESUME_LAST!"=="1" goto run_last

:menu
call :render_menu
choice /C 123WAQ /N /M "Select option: "
set "MENU_CHOICE=%ERRORLEVEL%"

if "%MENU_CHOICE%"=="1" goto run_all
if "%MENU_CHOICE%"=="2" goto run_last
if "%MENU_CHOICE%"=="3" goto choose_agents
if "%MENU_CHOICE%"=="4" goto toggle_workbench
if "%MENU_CHOICE%"=="5" goto toggle_api_gateway
if "%MENU_CHOICE%"=="6" goto quit
goto menu

:toggle_workbench
if "!WORKBENCH_LAUNCH!"=="1" (
    set "WORKBENCH_LAUNCH=0"
) else (
    set "WORKBENCH_LAUNCH=1"
)
goto menu

:toggle_api_gateway
if "!API_GATEWAY_LAUNCH!"=="1" (
    set "API_GATEWAY_LAUNCH=0"
) else (
    set "API_GATEWAY_LAUNCH=1"
)
goto menu

:run_all
set "PY_ARGS="
set "START_LABEL=all active agents"
>"%STATE_FILE%" echo all^|
goto launch

:run_last
if /i "!LAST_MODE!"=="all" (
    set "PY_ARGS="
    set "START_LABEL=all active agents ^(same as last time^)"
    goto launch
)
if /i "!LAST_MODE!"=="selected" if defined LAST_AGENTS (
    set "PY_ARGS=--agents !LAST_AGENTS!"
    set "START_LABEL=!LAST_AGENTS! ^(same as last time^)"
    goto launch
)
echo.
echo No saved previous selection yet. Falling back to all active agents.
timeout /t 2 /nobreak >nul
goto run_all

:choose_agents
cls
call :print_banner "AGENT SELECTION" "Choose one or more active agents"
for /l %%I in (1,1,!AGENT_COUNT!) do (
    call set "ENTRY=%%AGENT_LABEL_%%I%%"
    call :mark_last %%I
    echo   !C_ACCENT![%%I]!C_RESET! !C_TEXT!!ENTRY!!LAST_MARK!!C_RESET!
)
echo.
set "SELECTED_AGENTS="
set /p CHOICE_LIST=!C_MUTED!Enter one or more numbers separated by spaces: !C_RESET!
if not defined CHOICE_LIST goto menu
set "CHOICE_LIST=!CHOICE_LIST:,= !"

for %%N in (!CHOICE_LIST!) do call :append_agent %%N

if not defined SELECTED_AGENTS (
    echo.
    echo !C_WARN!No valid agents selected.!C_RESET!
    timeout /t 2 /nobreak >nul
    goto menu
)

set "PY_ARGS=--agents !SELECTED_AGENTS!"
set "START_LABEL=!SELECTED_AGENTS!"
>"%STATE_FILE%" echo selected^|!SELECTED_AGENTS!
goto launch

:launch
cls
call :print_banner "BRIDGE-U-F BOOT" "Multi-backend orchestrator launch"
echo !C_RAIL!│!C_RESET! !C_LABEL!Agents           !C_RESET! !C_TEXT!!START_LABEL!!C_RESET!
if "!WORKBENCH_LAUNCH!"=="1" (
    echo !C_RAIL!│!C_RESET! !C_LABEL!Workbench       !C_RESET! !C_OK!starting in background!C_RESET!
    start /MIN "" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench_ctl.ps1" -Action start -OpenBrowser
    set "AUTO_STOP_WORKBENCH=1"
) else (
    echo !C_RAIL!│!C_RESET! !C_LABEL!Workbench       !C_RESET! !C_MUTED!disabled!C_RESET!
)
if "!API_GATEWAY_LAUNCH!"=="1" (
    echo !C_RAIL!│!C_RESET! !C_LABEL!API Gateway     !C_RESET! !C_OK!enabled ^(port 18801^)!C_RESET!
) else (
    echo !C_RAIL!│!C_RESET! !C_LABEL!API Gateway     !C_RESET! !C_MUTED!disabled!C_RESET!
)
if "!DRY_RUN!"=="1" (
    echo !C_RAIL!│!C_RESET! !C_LABEL!Launch mode     !C_RESET! !C_WARN!dry run only!C_RESET!
    echo !C_RAIL!│!C_RESET!
    echo.
    exit /b 0
)
call :preflight_check
if errorlevel 1 (
    echo.
    echo !C_WARN!Launch aborted — bridge-u-f is already running.!C_RESET!
    if "!NO_PAUSE!"=="0" pause
    exit /b 1
)
echo !C_RAIL!│!C_RESET! !C_LABEL!Bridge launch    !C_RESET! !C_OK!proceeding!C_RESET!
echo !C_RAIL!│!C_RESET!
if exist "%AGENTS_FILE%" del "%AGENTS_FILE%" >nul 2>&1
set "GW_ARG="
if "!API_GATEWAY_LAUNCH!"=="1" set "GW_ARG=--api-gateway"
:: Ensure project root is on sys.path (required for embedded Python which doesn't add cwd)
set "PYTHONPATH=!BRIDGE_CODE_ROOT!"
call :resolve_wakeup_file
if defined WAKEUP_FILE call :start_wakeup_injector
"!PYTHON_EXE!" main.py --bridge-home "%BRIDGE_HOME%" %PY_ARGS% !GW_ARG!
if "!AUTO_STOP_WORKBENCH!"=="1" (
    echo.
    echo !C_MUTED!Stopping workbench services started by this launcher...!C_RESET!
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench_ctl.ps1" -Action stop >nul 2>&1
)
if "!NO_PAUSE!"=="1" exit /b 0
pause
exit /b 0

:resolve_wakeup_file
set "WAKEUP_FILE="
if exist "%BRIDGE_HOME%\workspaces\hashiko\WAKEUP.prompt" (
    set "WAKEUP_FILE=%BRIDGE_HOME%\workspaces\hashiko\WAKEUP.prompt"
    exit /b 0
)
if exist "%BRIDGE_HOME%\workspaces\onboarding_agent\WAKEUP.prompt" (
    set "WAKEUP_FILE=%BRIDGE_HOME%\workspaces\onboarding_agent\WAKEUP.prompt"
)
exit /b 0

:start_wakeup_injector
set "WAKEUP_PORT=%WORKBENCH_PORT%"
if not defined WAKEUP_PORT set "WAKEUP_PORT=18800"
start "" /MIN powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$wakeupFile = '%WAKEUP_FILE%'; " ^
  "$health = 'http://localhost:%WAKEUP_PORT%/api/health'; " ^
  "$chat = 'http://localhost:%WAKEUP_PORT%/api/chat'; " ^
  "for ($i = 0; $i -lt 40; $i++) { " ^
  "  Start-Sleep -Seconds 1; " ^
  "  try { " ^
  "    Invoke-RestMethod -Uri $health -Method Get -TimeoutSec 2 | Out-Null; " ^
  "    Start-Sleep -Seconds 1; " ^
  "    if (-not (Test-Path $wakeupFile)) { break }; " ^
  "    $prompt = Get-Content $wakeupFile -Raw; " ^
  "    $body = @{ agent = 'hashiko'; text = $prompt } | ConvertTo-Json -Compress; " ^
  "    Invoke-RestMethod -Uri $chat -Method Post -ContentType 'application/json' -Body $body | Out-Null; " ^
  "    Remove-Item $wakeupFile -Force; " ^
  "    break; " ^
  "  } catch { } " ^
  "}"
exit /b 0

:quit
if exist "%AGENTS_FILE%" del "%AGENTS_FILE%" >nul 2>&1
if exist "%INACTIVE_FILE%" del "%INACTIVE_FILE%" >nul 2>&1
exit /b 0

:render_menu
cls
set "WORKBENCH_LABEL=OFF"
if "!WORKBENCH_LAUNCH!"=="1" set "WORKBENCH_LABEL=ON"
set "API_GATEWAY_LABEL=OFF"
if "!API_GATEWAY_LAUNCH!"=="1" set "API_GATEWAY_LABEL=ON"
call :print_banner "BRIDGE-U-F LAUNCHER" "Universal bridge + optional workbench"
echo !C_RAIL!│!C_RESET! !C_LABEL!Active agents    !C_RESET! !C_TEXT!!AGENT_COUNT!!C_RESET!
echo !C_RAIL!│!C_RESET! !C_LABEL!Inactive agents  !C_RESET! !C_TEXT!!INACTIVE_COUNT!!C_RESET!
if "!WORKBENCH_LABEL!"=="ON" (
    echo !C_RAIL!│!C_RESET! !C_LABEL!Workbench       !C_RESET! !C_OK!ON!C_RESET!
) else (
    echo !C_RAIL!│!C_RESET! !C_LABEL!Workbench       !C_RESET! !C_MUTED!OFF!C_RESET!
)
if "!API_GATEWAY_LABEL!"=="ON" (
    echo !C_RAIL!│!C_RESET! !C_LABEL!API Gateway     !C_RESET! !C_OK!ON!C_RESET!
) else (
    echo !C_RAIL!│!C_RESET! !C_LABEL!API Gateway     !C_RESET! !C_MUTED!OFF!C_RESET!
)
echo !C_RAIL!│!C_RESET!
echo.
echo !C_ACCENT!Active Roster!C_RESET!
for /l %%I in (1,1,!AGENT_COUNT!) do (
    call set "ENTRY=%%AGENT_LABEL_%%I%%"
    call :mark_last %%I
    echo   !C_ACCENT![%%I]!C_RESET! !C_TEXT!!ENTRY!!LAST_MARK!!C_RESET!
)
echo.
echo !C_ACCENT!Inactive Roster!C_RESET!
if !INACTIVE_COUNT! GTR 0 (
    for /l %%I in (1,1,!INACTIVE_COUNT!) do (
        echo   !C_MUTED!- !INACTIVE_LABEL_%%I!!C_RESET!
    )
) else (
    echo   !C_MUTED!- none!C_RESET!
)
echo.
echo !C_ACCENT!Actions!C_RESET!
echo   !C_ACCENT![1]!C_RESET! Start all active agents
echo   !C_ACCENT![2]!C_RESET! Start same as last time
echo   !C_ACCENT![3]!C_RESET! Choose agents now
echo   !C_ACCENT![W]!C_RESET! Toggle workbench launch
echo   !C_ACCENT![A]!C_RESET! Toggle API gateway (port 18801)
echo   !C_ACCENT![Q]!C_RESET! Quit
echo.
exit /b 0

:init_theme
for /f %%E in ('echo prompt $E^| cmd') do set "ESC=%%E"
set "C_RESET=!ESC![0m"
set "C_ACCENT=!ESC![38;5;111m"
set "C_OK=!ESC![38;5;114m"
set "C_WARN=!ESC![38;5;222m"
set "C_LABEL=!ESC![38;5;109m"
set "C_TEXT=!ESC![97m"
set "C_MUTED=!ESC![90m"
set "C_RAIL=!ESC![38;5;61m"
set "C_TITLE=!ESC![1;38;5;153m"
exit /b 0

:print_banner
set "B_TITLE=%~1"
set "B_SUB=%~2"
echo.
echo !C_RAIL!│!C_RESET! !C_TITLE!!B_TITLE!!C_RESET!  !C_MUTED!!B_SUB!!C_RESET!
echo !C_RAIL!│!C_RESET!
exit /b 0

:ensure_env
if "!USING_EMBEDDED!"=="1" (
    :: USB mode — embedded Python has all packages pre-installed, skip venv entirely
    "!PYTHON_EXE!" -c "import telegram, httpx, aiohttp, PIL" >nul 2>&1
    if errorlevel 1 (
        echo !C_WARN!Embedded Python is missing required packages.!C_RESET!
        echo !C_MUTED!Run prepare_usb.bat again to reinstall dependencies.!C_RESET!
        exit /b 1
    )
    exit /b 0
)
if not exist .venv (
    echo !C_MUTED!Creating virtual environment...!C_RESET!
    python -m venv .venv || exit /b 1
)

call .venv\Scripts\activate.bat || exit /b 1

python -c "import telegram, httpx, aiohttp, PIL" >nul 2>&1
if errorlevel 1 (
    echo !C_MUTED!Installing Python dependencies...!C_RESET!
    pip install python-telegram-bot httpx aiohttp pillow || exit /b 1
)
exit /b 0

:load_agents
if exist "%AGENTS_FILE%" del "%AGENTS_FILE%" >nul 2>&1
if exist "%INACTIVE_FILE%" del "%INACTIVE_FILE%" >nul 2>&1
powershell -NoProfile -Command ^
  "$cfgPath = Join-Path $env:BRIDGE_HOME 'agents.json'; " ^
  "if (-not (Test-Path $cfgPath)) { $cfgPath = Join-Path $env:BRIDGE_CODE_ROOT 'agents.json' }; " ^
  "$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json; " ^
  "$active = $cfg.agents | Where-Object { $_.is_active -ne $false }; " ^
  "$inactive = $cfg.agents | Where-Object { $_.is_active -eq $false }; " ^
  "$active | ForEach-Object { '{0}|{1}' -f $_.name, ($(if ($_.type -eq 'flex') { $_.active_backend } elseif ($_.engine) { $_.engine } else { $_.type })) } | Set-Content '%AGENTS_FILE%'; " ^
  "$inactive | ForEach-Object { '{0}|{1}' -f $_.name, ($(if ($_.type -eq 'flex') { $_.active_backend } elseif ($_.engine) { $_.engine } else { $_.type })) } | Set-Content '%INACTIVE_FILE%'"

if not exist "%AGENTS_FILE%" (
    echo !C_WARN!Failed to read active agents from agents.json.!C_RESET!
    pause
    exit /b 1
)

set /a AGENT_COUNT=0
for /f "usebackq tokens=1,2 delims=|" %%A in ("%AGENTS_FILE%") do (
    set /a AGENT_COUNT+=1
    set "AGENT_!AGENT_COUNT!=%%A"
    set "AGENT_LABEL_!AGENT_COUNT!=%%A  [%%B]"
)

set /a INACTIVE_COUNT=0
if exist "%INACTIVE_FILE%" (
    for /f "usebackq tokens=1,2 delims=|" %%A in ("%INACTIVE_FILE%") do (
        set /a INACTIVE_COUNT+=1
        set "INACTIVE_!INACTIVE_COUNT!=%%A"
        set "INACTIVE_LABEL_!INACTIVE_COUNT!=%%A  [%%B]"
    )
)

if !AGENT_COUNT! LSS 1 (
    echo !C_WARN!No active agents found in agents.json.!C_RESET!
    pause
    exit /b 1
)
exit /b 0

:load_last_state
set "LAST_MODE="
set "LAST_AGENTS="
if exist "%STATE_FILE%" (
    for /f "usebackq tokens=1,* delims=|" %%A in ("%STATE_FILE%") do (
        set "LAST_MODE=%%A"
        set "LAST_AGENTS=%%B"
    )
)
exit /b 0

:append_agent
set "IDX=%~1"
for /f "delims=0123456789" %%X in ("%IDX%") do if not "%%X"=="" exit /b 0
if "%IDX%"=="" exit /b 0
if %IDX% LSS 1 exit /b 0
if %IDX% GTR !AGENT_COUNT! exit /b 0
call set "AGENT_NAME=%%AGENT_%IDX%%%"
if not defined AGENT_NAME exit /b 0
for %%Z in (!SELECTED_AGENTS!) do if /i "%%Z"=="!AGENT_NAME!" exit /b 0
if defined SELECTED_AGENTS (
    set "SELECTED_AGENTS=!SELECTED_AGENTS! !AGENT_NAME!"
) else (
    set "SELECTED_AGENTS=!AGENT_NAME!"
)
exit /b 0

:mark_last
set "IDX=%~1"
set "IS_LAST=0"
set "LAST_MARK="
call set "AGENT_NAME=%%AGENT_%IDX%%%"
if /i "!LAST_MODE!"=="all" set "IS_LAST=1"
if /i "!LAST_MODE!"=="selected" if defined LAST_AGENTS (
    for %%Z in (!LAST_AGENTS!) do (
        if /i "%%Z"=="!AGENT_NAME!" set "IS_LAST=1"
    )
)
if "!IS_LAST!"=="1" (
    set "LAST_MARK= !C_OK![last]!C_RESET!"
)
exit /b 0

:preflight_check
rem Check for an already-running bridge-u-f orchestrator.
rem Uses .bridge_u_f.pid (always readable) — the .lock file is unreadable while held.
set "EXISTING_PID="
if exist "%BRIDGE_HOME%\.bridge_u_f.pid" (
    for /f "usebackq delims=" %%P in ("%BRIDGE_HOME%\.bridge_u_f.pid") do set "EXISTING_PID=%%P"
)
if not defined EXISTING_PID exit /b 0
rem Verify the PID is still alive and is actually bridge-u-f
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = %EXISTING_PID%' -ErrorAction SilentlyContinue; " ^
  "if(-not $p){ exit 1 }; " ^
  "$cmd = [string]$p.CommandLine; " ^
  "if($cmd -and $cmd -like '*main.py*'){ exit 0 } else { exit 1 }"
if errorlevel 1 (
    rem PID file is stale — process is gone or not bridge-u-f
    del /f /q "%BRIDGE_HOME%\.bridge_u_f.pid" >nul 2>&1
    exit /b 0
)
echo.
echo   !C_WARN!Bridge-u-f is already running ^(PID !EXISTING_PID!^).!C_RESET!
if "!AUTO_RESUME_LAST!"=="1" (
    rem Called from restart script — don't prompt, just fail
    exit /b 1
)
echo.
choice /C YN /N /M "  Kill existing instance and continue? [Y/N] "
if errorlevel 2 exit /b 1
echo.
echo   !C_MUTED!Stopping existing instance...!C_RESET!
call "%~dp0kill_bridge_u_f_sessions.bat" --quiet --no-pause
if errorlevel 1 (
    echo   !C_WARN!Could not stop existing instance.!C_RESET!
    exit /b 1
)
timeout /t 2 /nobreak >nul
exit /b 0

:parse_args
if "%~1"=="" exit /b 0
if /i "%~1"=="--resume-last" set "AUTO_RESUME_LAST=1"
if /i "%~1"=="--workbench" set "WORKBENCH_LAUNCH=1"
if /i "%~1"=="--api-gateway" set "API_GATEWAY_LAUNCH=1"
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"
if /i "%~1"=="--dry-run" set "DRY_RUN=1"
shift
goto parse_args
