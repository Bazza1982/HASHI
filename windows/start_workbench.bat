@echo off
chcp 65001 >nul
setlocal

:: ============================================================
:: HASHI9 - Start Workbench
:: Delegates to bin\bridge-u.bat with --workbench flag.
:: Reads Workbench port from agents.json.
:: Logs written to: windows\logs\workbench_<timestamp>.log
:: ============================================================

set ROOT=%~dp0..
set LOG_DIR=%~dp0logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f %%p in ('powershell -NoProfile -Command "$cfg = Get-Content '%ROOT%\agents.json' -Raw | ConvertFrom-Json; if ($cfg.global.workbench_port) { $cfg.global.workbench_port } else { 18800 }"') do set WB_PORT=%%p
if not defined WB_PORT set WB_PORT=18800

for /f "tokens=1-3 delims=/ " %%a in ("%DATE%") do set D=%%a-%%b-%%c
for /f "tokens=1-3 delims=:." %%a in ("%TIME: =0%") do set T=%%a%%b%%c
set LOG_FILE=%LOG_DIR%\workbench_%D%_%T%.log

echo ==================================================== >> "%LOG_FILE%"
echo HASHI9 Workbench >> "%LOG_FILE%"
echo Port: %WB_PORT% >> "%LOG_FILE%"
echo Started: %DATE% %TIME% >> "%LOG_FILE%"
echo ==================================================== >> "%LOG_FILE%"

echo Starting HASHI9 with Workbench enabled (port %WB_PORT%)...
echo Log file: %LOG_FILE%
echo.

cd /d "%ROOT%"

powershell -Command "& { $env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; cmd /c 'bin\bridge-u.bat --resume-last --workbench' 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append }"
set EXIT_CODE=%ERRORLEVEL%

echo. >> "%LOG_FILE%"
echo Exit code: %EXIT_CODE% >> "%LOG_FILE%"
echo Stopped: %DATE% %TIME% >> "%LOG_FILE%"

echo.
echo Log saved to: %LOG_FILE%
pause
endlocal
