@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ============================================================
rem HASHI9 USB Packager
rem Builds a fully self-contained HASHI9 installation on D:\HASHI9
rem Run this ONCE on the host machine before distributing the USB.
rem Requirements: internet connection (downloads Python + packages)
rem ============================================================

set TARGET=D:\HASHI9
for %%d in ("%~dp0..") do set SOURCE=%%~fd
set PYTHON_VERSION=3.12.13
set PBS_DATE=20260303
set PYTHON_ARCHIVE=cpython-%PYTHON_VERSION%+%PBS_DATE%-x86_64-pc-windows-msvc-install_only_stripped.tar.gz
set PYTHON_URL=https://github.com/astral-sh/python-build-standalone/releases/download/%PBS_DATE%/%PYTHON_ARCHIVE%
set PYTHON_DIR=%TARGET%\python

echo.
echo ============================================================
echo  HASHI9 USB Packager
echo  Target: %TARGET%
echo ============================================================
echo.

if not exist "D:\" (
    echo ERROR: D:\ not found. Please insert USB drive as D: and retry.
    pause
    exit /b 1
)

echo This will build a self-contained HASHI9 at %TARGET%
echo Existing contents at that path will be overwritten.
echo.
set /p CONFIRM=Type YES to continue:
if /i not "%CONFIRM%"=="YES" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo [1/6] Copying project files...

if exist "%TARGET%\python" (
    echo    Keeping existing Python installation...
) else (
    if exist "%TARGET%" rmdir /s /q "%TARGET%"
)
if not exist "%TARGET%" mkdir "%TARGET%"

robocopy "%SOURCE%" "%TARGET%" /E /R:0 /W:0 /XD .git .venv __pycache__ build dist logs wa_session windows-packaging-smoke-home node_modules .idea .vscode /XF *.pyc *.pyo *.spec hashi-zero.exe /NP /NFL /NDL /NJH /NJS >nul 2>&1

if errorlevel 16 (
    echo ERROR: File copy failed. Check that D: is writable.
    pause
    exit /b 1
)
echo    Done.

echo.
echo [2/6] Preparing portable CPython %PYTHON_VERSION%...
if exist "%PYTHON_DIR%\python.exe" (
    "%PYTHON_DIR%\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:3] == tuple(map(int, '%PYTHON_VERSION%'.split('.'))) else 1)" >nul 2>&1
    if not errorlevel 1 (
        echo    Approved Python already present, skipping download.
        goto :install_pip
    )
    echo    Existing Python does not match %PYTHON_VERSION%; replacing it.
    rmdir /s /q "%PYTHON_DIR%"
)

if not exist "%TARGET%\tmp" mkdir "%TARGET%\tmp"
powershell -NoProfile -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%TARGET%\tmp\%PYTHON_ARCHIVE%' -UseBasicParsing"
if errorlevel 1 (
    echo ERROR: Failed to download Python. Check internet connection.
    pause
    exit /b 1
)

echo    Extracting...
if not exist "%PYTHON_DIR%" mkdir "%PYTHON_DIR%"
tar -xzf "%TARGET%\tmp\%PYTHON_ARCHIVE%" -C "%PYTHON_DIR%" --strip-components=1
if errorlevel 1 (
    echo ERROR: Failed to extract portable Python. Windows tar is required.
    pause
    exit /b 1
)
rmdir /s /q "%TARGET%\tmp"
echo    Done.

:install_pip
echo.
echo [3/6] Enabling pip in portable Python...
"%PYTHON_DIR%\python.exe" -m ensurepip --upgrade --default-pip >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python ensurepip failed.
    pause
    exit /b 1
)
echo    Done.

echo.
echo [4/6] Installing the approved dependency generation...
"%PYTHON_DIR%\python.exe" -m pip install -r "%TARGET%\constraints\standard-py312.lock" --no-warn-script-location --quiet

if errorlevel 1 (
    echo ERROR: Package installation failed.
    pause
    exit /b 1
)
"%PYTHON_DIR%\python.exe" "%TARGET%\scripts\check_runtime_contract.py"
if errorlevel 1 (
    echo ERROR: Installed runtime does not satisfy the HASHI Core contract.
    pause
    exit /b 1
)
echo    Done.

echo.
echo [5/6] Stripping runtime data...

if exist "%TARGET%\logs" rmdir /s /q "%TARGET%\logs"
mkdir "%TARGET%\logs" >nul 2>&1

if exist "%TARGET%\wa_session" rmdir /s /q "%TARGET%\wa_session"
mkdir "%TARGET%\wa_session" >nul 2>&1

for /D %%W in ("%TARGET%\workspaces\*") do (
    if exist "%%W\transcript.jsonl"         del /f /q "%%W\transcript.jsonl"
    if exist "%%W\conversation_log.jsonl"   del /f /q "%%W\conversation_log.jsonl"
    if exist "%%W\recent_context.jsonl"     del /f /q "%%W\recent_context.jsonl"
    if exist "%%W\handoff.md"               del /f /q "%%W\handoff.md"
    if exist "%%W\bridge_memory.sqlite"     del /f /q "%%W\bridge_memory.sqlite"
    if exist "%%W\bridge_memory.sqlite-wal" del /f /q "%%W\bridge_memory.sqlite-wal"
    if exist "%%W\bridge_memory.sqlite-shm" del /f /q "%%W\bridge_memory.sqlite-shm"
    if exist "%%W\state.json"               del /f /q "%%W\state.json"
    if exist "%%W\logs"                     rmdir /s /q "%%W\logs"
    if exist "%%W\tui_onboarding_complete"  del /f /q "%%W\tui_onboarding_complete"
)

echo    Done.

echo.
echo [6/6] Configuring DeepSeek API for China build...

powershell -NoProfile -Command "$f='%TARGET%\agents.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; foreach($a in $j.agents){ if($a.name -eq 'hashiko'){ $a.engine='deepseek-api'; $a.model='deepseek-v4-flash'; $a.active_backend='deepseek-api'; $a.allowed_backends=@(@{engine='deepseek-api';model='deepseek-v4-flash'}) } }; if($j.global.authorized_id -isnot [int]){ $j.global.authorized_id=0 }; $j | ConvertTo-Json -Depth 10 | Set-Content $f -Encoding UTF8"

powershell -NoProfile -Command "$f='%TARGET%\secrets.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; $deepseekKey=$j.'deepseek-api_key'; if(-not $deepseekKey){ $deepseekKey=$j.'deepseek_api_key' }; $j.PSObject.Properties.Remove('openrouter-api_key'); if($deepseekKey){ Add-Member -InputObject $j -MemberType NoteProperty -Name 'deepseek-api_key' -Value $deepseekKey -Force } else { Write-Host 'WARNING: deepseek-api_key not found in secrets.json; configure it manually before use.' }; $j | ConvertTo-Json -Depth 5 | Set-Content $f -Encoding UTF8"

powershell -NoProfile -Command "$f='%TARGET%\agents.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; $j.global.workbench_port=8779; $j | ConvertTo-Json -Depth 10 | Set-Content $f -Encoding UTF8"

powershell -NoProfile -Command "$f='%TARGET%\bin\bridge-u.bat'; (Get-Content $f) -replace 'if not defined BRIDGE_HOME set \""BRIDGE_HOME=!BRIDGE_CODE_ROOT!\""',':: USB: always force BRIDGE_HOME to this USB root`r`nset \""BRIDGE_HOME=!BRIDGE_CODE_ROOT!\""' | Set-Content $f -Encoding UTF8"

echo    Done.

echo.
echo ============================================================
echo  USB package built successfully at %TARGET%
echo.
echo  First run:  D:\HASHI9\windows\TUI_onboarding.bat
echo  After that: D:\HASHI9\windows\start_tui.bat
echo.
echo  No Python installation required on the target machine.
echo ============================================================
echo.
pause
endlocal
