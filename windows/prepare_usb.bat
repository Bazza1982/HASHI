@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: ============================================================
:: HASHI9 USB Packager
:: Builds a fully self-contained HASHI9 installation on D:\HASHI9
:: Run this ONCE on the host machine before distributing the USB.
:: Requirements: internet connection (downloads Python + packages)
:: ============================================================

set TARGET=D:\HASHI9
set SOURCE=%~dp0..
set PYTHON_VERSION=3.13.3
set PYTHON_ZIP=python-%PYTHON_VERSION%-embed-amd64.zip
set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/%PYTHON_ZIP%
set PYTHON_DIR=%TARGET%\python
set GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py

echo.
echo ============================================================
echo  HASHI9 USB Packager
echo  Target: %TARGET%
echo ============================================================
echo.

:: Confirm target drive exists
if not exist "D:\" (
    echo ERROR: D:\ not found. Please insert USB drive as D: and retry.
    pause
    exit /b 1
)

:: Warn user
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
echo [1/5] Copying project files...

:: Clear previous HASHI9 folder (except python\ to avoid re-download)
if exist "%TARGET%\python" (
    echo    Keeping existing Python installation...
) else (
    if exist "%TARGET%" rmdir /s /q "%TARGET%"
)
if not exist "%TARGET%" mkdir "%TARGET%"

:: Copy project files, excluding runtime/dev artifacts
:: /R:0 /W:0 = no retries on locked files (avoids hanging on open SQLite DBs)
:: errorlevel 16 = fatal; errorlevel 8 = some files skipped (acceptable)
robocopy "%SOURCE%" "%TARGET%" /E /R:0 /W:0 ^
    /XD .git .venv __pycache__ build dist logs wa_session ^
    windows-packaging-smoke-home node_modules .idea .vscode ^
    /XF *.pyc *.pyo *.spec hashi-zero.exe ^
    /NP /NFL /NDL /NJH /NJS >nul 2>&1

if errorlevel 16 (
    echo ERROR: File copy failed. Check that D: is writable.
    pause
    exit /b 1
)
echo    Done.

echo.
echo [2/5] Downloading Python %PYTHON_VERSION% embeddable...
if exist "%PYTHON_DIR%\python.exe" (
    echo    Python already present, skipping download.
    goto :install_pip
)

if not exist "%TARGET%\tmp" mkdir "%TARGET%\tmp"
powershell -NoProfile -Command ^
    "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%TARGET%\tmp\%PYTHON_ZIP%' -UseBasicParsing"
if errorlevel 1 (
    echo ERROR: Failed to download Python. Check internet connection.
    pause
    exit /b 1
)

echo    Extracting...
if not exist "%PYTHON_DIR%" mkdir "%PYTHON_DIR%"
powershell -NoProfile -Command ^
    "Expand-Archive -Path '%TARGET%\tmp\%PYTHON_ZIP%' -DestinationPath '%PYTHON_DIR%' -Force"
rmdir /s /q "%TARGET%\tmp"
echo    Done.

:install_pip
echo.
echo [3/5] Enabling pip in embedded Python...

:: Enable site-packages and modify the ._pth file
set PTH_FILE=
for %%f in ("%PYTHON_DIR%\python*._pth") do set PTH_FILE=%%f

if "%PTH_FILE%"=="" (
    echo ERROR: Could not find Python ._pth file in %PYTHON_DIR%
    pause
    exit /b 1
)

:: Write updated .pth: add project root (..) and enable site-packages
:: IMPORTANT: when ._pth exists, Python ignores PYTHONPATH entirely.
:: ".." resolves to the parent of python\ which is the project root.
(
    echo python313.zip
    echo .
    echo ..
    echo import site
) > "%PTH_FILE%"

:: Download get-pip.py
powershell -NoProfile -Command ^
    "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%PYTHON_DIR%\get-pip.py' -UseBasicParsing"
if errorlevel 1 (
    echo ERROR: Failed to download get-pip.py
    pause
    exit /b 1
)

"%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\get-pip.py" --no-warn-script-location >nul 2>&1
del "%PYTHON_DIR%\get-pip.py"
echo    Done.

echo.
echo [4/5] Installing Python packages (this may take a few minutes)...
"%PYTHON_DIR%\python.exe" -m pip install ^
    "python-telegram-bot>=20.0" ^
    "httpx>=0.24.0" ^
    "aiohttp>=3.8.0" ^
    "pillow>=9.0.0" ^
    "rich>=13.0.0" ^
    "textual>=0.50.0" ^
    "edge-tts>=6.0.0" ^
    "psutil>=5.9.0" ^
    --no-warn-script-location --quiet

if errorlevel 1 (
    echo ERROR: Package installation failed.
    pause
    exit /b 1
)
echo    Done.

echo.
echo [5/5] Finalising - stripping runtime data...

:: System logs
if exist "%TARGET%\logs" rmdir /s /q "%TARGET%\logs"
mkdir "%TARGET%\logs" >nul 2>&1

:: WhatsApp session (contains linked account - do not ship)
if exist "%TARGET%\wa_session" rmdir /s /q "%TARGET%\wa_session"
mkdir "%TARGET%\wa_session" >nul 2>&1

:: Workspace runtime files - keep only AGENT.md per workspace
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
)

echo    Done.

echo.
echo [6/6] Configuring DeepSeek API for China build...

:: Patch agents.json: set hashiko to use deepseek-api / deepseek-reasoner
:: Also fix authorized_id: must be integer 0 (not string placeholder) for config to load
powershell -NoProfile -Command ^
    "$f='%TARGET%\agents.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; foreach($a in $j.agents){ if($a.name -eq 'hashiko'){ $a.engine='deepseek-api'; $a.model='deepseek-reasoner'; $a.active_backend='deepseek-api'; $a.allowed_backends=@(@{engine='deepseek-api';model='deepseek-reasoner'}) } }; if($j.global.authorized_id -isnot [int]){ $j.global.authorized_id=0 }; $j | ConvertTo-Json -Depth 10 | Set-Content $f -Encoding UTF8"

:: Patch secrets.json: keep deepseek key only if it already exists in copied secrets.json
:: Never embed live credentials in this script or any tracked file.
powershell -NoProfile -Command ^
    "$f='%TARGET%\secrets.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; $deepseekKey=$j.'deepseek-api_key'; if(-not $deepseekKey){ $deepseekKey=$j.'deepseek_api_key' }; $j.PSObject.Properties.Remove('openrouter-api_key'); if($deepseekKey){ Add-Member -InputObject $j -MemberType NoteProperty -Name 'deepseek-api_key' -Value $deepseekKey -Force } else { Write-Host 'WARNING: deepseek-api_key not found in secrets.json; configure it manually before use.' }; $j | ConvertTo-Json -Depth 5 | Set-Content $f -Encoding UTF8"

:: Patch agents.json: use distinct workbench port (8779) to avoid conflict with host instance
powershell -NoProfile -Command ^
    "$f='%TARGET%\agents.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; $j.global.workbench_port=8779; $j | ConvertTo-Json -Depth 10 | Set-Content $f -Encoding UTF8"

:: Fix bridge-u.bat: force BRIDGE_HOME to USB root (ignore inherited env from host HASHI9)
powershell -NoProfile -Command ^
    "$f='%TARGET%\bin\bridge-u.bat'; (Get-Content $f) -replace 'if not defined BRIDGE_HOME set \"BRIDGE_HOME=!BRIDGE_CODE_ROOT!\"',':: USB: always force BRIDGE_HOME to this USB root`r`nset \"BRIDGE_HOME=!BRIDGE_CODE_ROOT!\"' | Set-Content $f -Encoding UTF8"

echo    Done.

echo.
echo ============================================================
echo  USB package built successfully at %TARGET%
echo.
echo  To start HASHI9 on any Windows PC:
echo    Double-click:  D:\HASHI9\windows\start_tui.bat
echo.
echo  No Python installation required on the target machine.
echo ============================================================
echo.
pause
endlocal
