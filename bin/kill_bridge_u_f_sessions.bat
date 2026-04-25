@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "QUIET=0"
set "NO_PAUSE=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--quiet" set "QUIET=1"
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"
shift
goto parse_args

:args_done
if "!QUIET!"=="0" (
    cls
    echo ================================================================
    echo                 KILL BRIDGE-U-F REMAINING SESSIONS
    echo ================================================================
    echo.
    echo This will force-stop bridge-u-f bridge processes and their children,
    echo stop workbench services, and only remove the lock if nothing remains.
    echo.
)

if exist "%~dp0workbench_ctl.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench_ctl.ps1" -Action stop >nul 2>&1
)

if exist "%~dp0bridge_ctl.ps1" (
    if "!QUIET!"=="1" (
        powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bridge_ctl.ps1" -Action kill -Quiet >nul 2>&1
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bridge_ctl.ps1" -Action kill
    )
    set "PS_EXIT=%ERRORLEVEL%"
    if "%PS_EXIT%"=="0" exit /b 0
)

rem NOTE: On Windows, the venv python.exe shim spawns a child python.exe,
rem so a single bridge-u-f instance appears as TWO python.exe processes
rem (both with "main.py" in the command line). This is normal.
rem The tree-walk below handles this correctly.
set "PID_FILE=%TEMP%\bridge_u_f_kill_pids.txt"
if exist "%PID_FILE%" del "%PID_FILE%" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$project = (Resolve-Path '.').Path; " ^
  "$mainPath = Join-Path $project 'main.py'; " ^
  "$bridgeHome = [System.IO.Path]::GetFullPath($project).TrimEnd('\'); " ^
  "$selfPid = [int]$PID; " ^
  "$procs = Get-CimInstance Win32_Process; " ^
  "$byPid = @{}; $children = @{}; " ^
  "foreach($p in $procs){ " ^
  "  $procId = [int]$p.ProcessId; $parentId = [int]$p.ParentProcessId; " ^
  "  $byPid[$procId] = $p; " ^
  "  if(-not $children.ContainsKey($parentId)){ $children[$parentId] = New-Object System.Collections.Generic.List[int] }; " ^
  "  $children[$parentId].Add($procId); " ^
  "}; " ^
  "$parentPid = 0; if($byPid.ContainsKey($selfPid)){ $parentPid = [int]$byPid[$selfPid].ParentProcessId }; " ^
  "$targets = New-Object System.Collections.Generic.HashSet[int]; " ^
  "$roots = New-Object System.Collections.Generic.Queue[int]; " ^
  "function Test-BridgeCmd([string]$cmd){ " ^
  "  if(-not $cmd){ return $false }; " ^
  "  if($cmd -match '(?i)--bridge-home\s+(""([^""]+)""|(\S+))'){ " ^
  "    $candidate = if($matches[2]){ $matches[2] } else { $matches[3] }; " ^
  "    try { $resolved = [System.IO.Path]::GetFullPath($candidate).TrimEnd('\') } catch { $resolved = $candidate.TrimEnd('\') }; " ^
  "    return $resolved -ieq $bridgeHome; " ^
  "  }; " ^
  "  return [regex]::IsMatch($cmd, '(?i)(^|[""\s])' + [regex]::Escape($mainPath) + '("|\s|$)'); " ^
  "}; " ^
  "function Add-Root([int]$rootProcId){ " ^
  "  if($rootProcId -gt 0 -and $rootProcId -ne $selfPid -and $rootProcId -ne $parentPid -and $targets.Add($rootProcId)){ $roots.Enqueue($rootProcId) } " ^
  "}; " ^
  "foreach($p in $procs){ " ^
  "  $procId = [int]$p.ProcessId; " ^
  "  if($procId -eq $selfPid -or $procId -eq $parentPid){ continue }; " ^
  "  $name = [string]$p.Name; " ^
  "  $cmd = [string]$p.CommandLine; " ^
  "  if($name -ieq 'python.exe' -and (Test-BridgeCmd $cmd)){ Add-Root $procId; continue }; " ^
  "  if($name -ieq 'py.exe' -and (Test-BridgeCmd $cmd)){ Add-Root $procId; continue }; " ^
  "  if($name -ieq 'cmd.exe' -and $cmd -and $cmd -like ('*' + $project + '\bridge-u.bat*')){ Add-Root $procId; continue } " ^
  "}; " ^
  "foreach($port in 18800,18801){ " ^
  "  try { " ^
  "    $owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; " ^
  "    foreach($owner in @($owners)){ if($owner){ Add-Root ([int]$owner) } } " ^
  "  } catch {} " ^
  "}; " ^
  "while($roots.Count -gt 0){ " ^
  "  $current = $roots.Dequeue(); " ^
  "  if($children.ContainsKey($current)){ " ^
  "    foreach($child in $children[$current]){ " ^
  "      if($child -ne $selfPid -and $child -ne $parentPid -and $targets.Add($child)){ $roots.Enqueue($child) } " ^
  "    } " ^
  "  } " ^
  "}; " ^
  "$targets | Sort-Object -Unique | Set-Content '%PID_FILE%'"

set "FOUND_ANY=0"
if exist "%PID_FILE%" (
    for /f "usebackq delims=" %%P in ("%PID_FILE%") do (
        if not "%%P"=="" (
            set "FOUND_ANY=1"
            if "!QUIET!"=="0" echo Stopping PID %%P ...
            taskkill /PID %%P /T /F >nul 2>&1
        )
    )
)

if "%FOUND_ANY%"=="0" (
    if "!QUIET!"=="0" echo No bridge-u-f bridge processes found.
) else (
    timeout /t 2 /nobreak >nul
    if "!QUIET!"=="0" echo Cleanup commands issued.
)

set "BRIDGE_STILL_RUNNING=0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$project = (Resolve-Path '.').Path; " ^
  "$mainPath = Join-Path $project 'main.py'; " ^
  "$bridgeHome = [System.IO.Path]::GetFullPath($project).TrimEnd('\'); " ^
  "function Test-BridgeCmd([string]$cmd){ " ^
  "  if(-not $cmd){ return $false }; " ^
  "  if($cmd -match '(?i)--bridge-home\s+(""([^""]+)""|(\S+))'){ " ^
  "    $candidate = if($matches[2]){ $matches[2] } else { $matches[3] }; " ^
  "    try { $resolved = [System.IO.Path]::GetFullPath($candidate).TrimEnd('\') } catch { $resolved = $candidate.TrimEnd('\') }; " ^
  "    return $resolved -ieq $bridgeHome; " ^
  "  }; " ^
  "  return [regex]::IsMatch($cmd, '(?i)(^|[""\s])' + [regex]::Escape($mainPath) + '("|\s|$)'); " ^
  "}; " ^
  "$alive = Get-CimInstance Win32_Process | Where-Object { " ^
  "  $name = [string]$_.Name; $cmd = [string]$_.CommandLine; " ^
  "  (($name -ieq 'python.exe' -or $name -ieq 'py.exe') -and (Test-BridgeCmd $cmd)) " ^
  "} | Select-Object -First 1; " ^
  "if($alive){ exit 1 } else { exit 0 }"
if errorlevel 1 set "BRIDGE_STILL_RUNNING=1"

if "%BRIDGE_STILL_RUNNING%"=="0" (
    if exist "%~dp0.bridge_u_f.lock" (
        del /f /q "%~dp0.bridge_u_f.lock" >nul 2>&1
        if "!QUIET!"=="0" echo Removed stale .bridge_u_f.lock
    )
    if exist "%~dp0.bridge_u_f.pid" (
        del /f /q "%~dp0.bridge_u_f.pid" >nul 2>&1
        if "!QUIET!"=="0" echo Removed stale .bridge_u_f.pid
    )
) else (
    if "!QUIET!"=="0" echo Bridge-U-F process still appears to be running; lock/pid files were kept.
)

if exist "%PID_FILE%" del "%PID_FILE%" >nul 2>&1

if "!QUIET!"=="0" (
    echo.
    if "%BRIDGE_STILL_RUNNING%"=="0" (
        echo Cleanup complete.
    ) else (
        echo Cleanup incomplete. Some bridge-u-f process may still be alive.
    )
    echo.
)

if "!NO_PAUSE!"=="0" if "!QUIET!"=="0" pause
if "%BRIDGE_STILL_RUNNING%"=="0" (
    exit /b 0
) else (
    exit /b 1
)
