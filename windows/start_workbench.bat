@echo off
chcp 65001 >nul
setlocal

:: ============================================================
:: HASHI9 - Start Workbench
:: Delegates to bin\bridge-u.bat with --workbench flag.
:: Port 8769 set in agents.json (avoids HASHI1/HASHI2 conflict).
:: Logs written to: windows\logs\workbench_<timestamp>.log
:: ============================================================

set ROOT=%~dp0..
set LOG_DIR=%~dp0logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=1-3 delims=/ " %%a in ("%DATE%") do set D=%%a-%%b-%%c
for /f "tokens=1-3 delims=:." %%a in ("%TIME: =0%") do set T=%%a%%b%%c
set LOG_FILE=%LOG_DIR%\workbench_%D%_%T%.log

echo ==================================================== >> "%LOG_FILE%"
echo HASHI9 Workbench >> "%LOG_FILE%"
echo Port: 8769 >> "%LOG_FILE%"
echo Started: %DATE% %TIME% >> "%LOG_FILE%"
echo ==================================================== >> "%LOG_FILE%"

echo Starting HASHI9 with Workbench enabled (port 8769)...
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
