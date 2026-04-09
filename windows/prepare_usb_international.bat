@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ============================================================
rem HASHI9 USB Packager -- INTERNATIONAL BUILD
rem Defaults: OpenRouter + claude-sonnet-4-6
rem Keeps ALL API keys (openrouter, deepseek, brave, future keys)
rem Removes personal info: telegram_user_id
rem Builds a fully self-contained HASHI9 installation on D:\HASHI9
rem Run this ONCE on the host machine before distributing the USB.
rem Requirements: internet connection (downloads Python + packages)
rem ============================================================

set TARGET=D:\HASHI9
for %%d in ("%~dp0..") do set SOURCE=%%~fd
set PYTHON_VERSION=3.13.3
set PYTHON_ZIP=python-%PYTHON_VERSION%-embed-amd64.zip
set PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/%PYTHON_ZIP%
set PYTHON_DIR=%TARGET%\python
set GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py

echo.
echo ============================================================
echo  HASHI9 USB Packager -- INTERNATIONAL BUILD
echo  Target: %TARGET%
echo  LLM:    OpenRouter / claude-sonnet-4-6
echo ============================================================
echo.

if not exist "D:\" (
    echo ERROR: D:\ not found. Please insert USB drive as D: and retry.
    pause
    exit /b 1
)

echo This will build a self-contained HASHI9 (International) at %TARGET%
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
echo [2/6] Downloading Python %PYTHON_VERSION% embeddable...
if exist "%PYTHON_DIR%\python.exe" (
    echo    Python already present, skipping download.
    goto :install_pip
)

if not exist "%TARGET%\tmp" mkdir "%TARGET%\tmp"
powershell -NoProfile -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%TARGET%\tmp\%PYTHON_ZIP%' -UseBasicParsing"
if errorlevel 1 (
    echo ERROR: Failed to download Python. Check internet connection.
    pause
    exit /b 1
)

echo    Extracting...
if not exist "%PYTHON_DIR%" mkdir "%PYTHON_DIR%"
powershell -NoProfile -Command "Expand-Archive -Path '%TARGET%\tmp\%PYTHON_ZIP%' -DestinationPath '%PYTHON_DIR%' -Force"
rmdir /s /q "%TARGET%\tmp"
echo    Done.

:install_pip
echo.
echo [3/6] Enabling pip in embedded Python...

set PTH_FILE=
for %%f in ("%PYTHON_DIR%\python*._pth") do set PTH_FILE=%%f

if "%PTH_FILE%"=="" (
    echo ERROR: Could not find Python ._pth file in %PYTHON_DIR%
    pause
    exit /b 1
)

(
    echo python313.zip
    echo .
    echo ..
    echo import site
) > "%PTH_FILE%"

powershell -NoProfile -Command "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%PYTHON_DIR%\get-pip.py' -UseBasicParsing"
if errorlevel 1 (
    echo ERROR: Failed to download get-pip.py
    pause
    exit /b 1
)

"%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\get-pip.py" --no-warn-script-location >nul 2>&1
del "%PYTHON_DIR%\get-pip.py"
echo    Done.

echo.
echo [4/6] Installing Python packages (this may take a few minutes)...
"%PYTHON_DIR%\python.exe" -m pip install "python-telegram-bot>=20.0" "httpx>=0.24.0" "aiohttp>=3.8.0" "pillow>=9.0.0" "rich>=13.0.0" "textual>=0.50.0" "edge-tts>=6.0.0" "psutil>=5.9.0" --no-warn-script-location --quiet

if errorlevel 1 (
    echo ERROR: Package installation failed.
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
echo [6/6] Configuring International build (OpenRouter / claude-sonnet-4-6)...

powershell -NoProfile -Command "$f='%TARGET%\agents.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; foreach($a in $j.agents){ if($a.name -eq 'hashiko'){ $a.engine='openrouter'; $a.model='claude-sonnet-4-6'; $a.active_backend='openrouter'; $a.allowed_backends=@(@{engine='openrouter';model='claude-sonnet-4-6'},@{engine='deepseek-api';model='deepseek-reasoner'}) } }; if($j.global.authorized_id -isnot [int]){ $j.global.authorized_id=0 }; $j | ConvertTo-Json -Depth 10 | Set-Content $f -Encoding UTF8"

powershell -NoProfile -Command "$f='%TARGET%\secrets.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; $j.PSObject.Properties.Remove('telegram_user_id'); $j | ConvertTo-Json -Depth 5 | Set-Content $f -Encoding UTF8"

powershell -NoProfile -Command "$f='%TARGET%\agents.json'; $j=Get-Content $f -Raw | ConvertFrom-Json; $j.global.workbench_port=8779; $j | ConvertTo-Json -Depth 10 | Set-Content $f -Encoding UTF8"

powershell -NoProfile -Command "$f='%TARGET%\bin\bridge-u.bat'; (Get-Content $f) -replace 'if not defined BRIDGE_HOME set \""BRIDGE_HOME=!BRIDGE_CODE_ROOT!\""',':: USB: always force BRIDGE_HOME to this USB root`r`nset \""BRIDGE_HOME=!BRIDGE_CODE_ROOT!\""' | Set-Content $f -Encoding UTF8"

echo    Done.

echo.
echo ============================================================
echo  USB package (International) built successfully at %TARGET%
echo.
echo  LLM: OpenRouter / claude-sonnet-4-6
echo  API Keys kept: openrouter, deepseek, brave (and all others)
echo  Personal info removed: telegram_user_id
echo.
echo  First run:  D:\HASHI9\windows\TUI_onboarding.bat
echo  After that: D:\HASHI9\windows\start_tui.bat
echo.
echo  No Python installation required on the target machine.
echo ============================================================
echo.
pause
endlocal
