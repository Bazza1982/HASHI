@echo off
setlocal EnableDelayedExpansion
set "BRIDGE_CODE_ROOT=%~dp0"
cd /d "%BRIDGE_CODE_ROOT%"
if not defined BRIDGE_HOME set "BRIDGE_HOME=%BRIDGE_CODE_ROOT%"

set "WORKBENCH_PORT=18800"
set "API_TOKEN="
set "PASS_ARGS=%*"

rem --- Read config ---
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$cfgPath = Join-Path $env:BRIDGE_HOME 'agents.json'; " ^
  "if (-not (Test-Path $cfgPath)) { $cfgPath = Join-Path $env:BRIDGE_CODE_ROOT 'agents.json' }; " ^
  "$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json; " ^
  "$port = $cfg.global.workbench_port; if(-not $port){ $port = 18800 }; Write-Output $port"`) do (
    set "WORKBENCH_PORT=%%P"
)

for /f "usebackq delims=" %%T in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$secretsPath = Join-Path $env:BRIDGE_HOME 'secrets.json'; " ^
  "if (-not (Test-Path $secretsPath)) { $secretsPath = Join-Path $env:BRIDGE_CODE_ROOT 'secrets.json' }; " ^
  "if(Test-Path $secretsPath){ " ^
  "  $s = Get-Content $secretsPath -Raw | ConvertFrom-Json; " ^
  "  if($s.workbench_admin_token){ Write-Output $s.workbench_admin_token } " ^
  "}"`) do (
    set "API_TOKEN=%%T"
)

rem --- Check if bridge is actually running ---
call :is_bridge_alive
if errorlevel 1 (
    echo No running bridge-u-f instance detected. Starting fresh...
    goto start_bridge
)

rem --- Try graceful shutdown via admin API ---
echo Requesting graceful bridge-u-f shutdown on port !WORKBENCH_PORT!...
set "API_OK=0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$headers = @{}; " ^
  "if('%API_TOKEN%' -ne ''){ $headers['X-Workbench-Token'] = '%API_TOKEN%' }; " ^
  "try { " ^
  "  Invoke-RestMethod -Method Post -Uri ('http://127.0.0.1:' + !WORKBENCH_PORT! + '/api/admin/shutdown') -Headers $headers -ContentType 'application/json' -Body '{\"reason\":\"restart-script\"}' | Out-Null; " ^
  "  exit 0 " ^
  "} catch { exit 1 }"
if not errorlevel 1 set "API_OK=1"

if "!API_OK!"=="1" (
    echo Shutdown API accepted. Waiting for process to exit...
) else (
    echo Shutdown API unreachable. Will force-kill after wait.
)

rem --- Poll for process exit (goto-based loop, max 30 iterations) ---
set /a WAIT_COUNT=0
set "WAIT_MAX=30"

:wait_loop
call :is_bridge_alive
if errorlevel 1 goto start_bridge
set /a WAIT_COUNT+=1
if !WAIT_COUNT! GEQ !WAIT_MAX! goto force_kill
timeout /t 1 /nobreak >nul
goto wait_loop

:force_kill
echo Graceful shutdown did not complete in %WAIT_MAX%s. Forcing cleanup...
call "%~dp0kill_bridge_u_f_sessions.bat" --quiet --no-pause
if errorlevel 1 (
    echo Forced cleanup failed. Bridge-U-F may still be running.
    exit /b 1
)
rem Give OS a moment to release resources after force-kill
timeout /t 2 /nobreak >nul

:start_bridge
echo Starting bridge-u-f again...
call "%~dp0bridge-u.bat" --resume-last --no-pause %PASS_ARGS%
exit /b %ERRORLEVEL%

:is_bridge_alive
rem Exit 0 if a bridge-u-f python process is alive, exit 1 if none found.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$project = (Resolve-Path '.').Path; " ^
  "$bridgeHome = $env:BRIDGE_HOME; " ^
  "$alive = Get-CimInstance Win32_Process | Where-Object { " ^
  "  $n = [string]$_.Name; $c = [string]$_.CommandLine; " ^
  "  (($n -ieq 'python.exe' -or $n -ieq 'py.exe') -and $c -and $c -like ('*' + $project + '*main.py*') -and (($c -like ('*--bridge-home*' + $bridgeHome + '*')) -or ($c -notlike '*--bridge-home*'))) " ^
  "} | Select-Object -First 1; " ^
  "if($alive){ exit 0 } else { exit 1 }"
exit /b %ERRORLEVEL%
