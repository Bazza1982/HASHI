@echo off
chcp 65001 >nul
setlocal

:: ============================================================
:: HASHI - Start Main Bridge
:: Delegates to bin\bridge-u.bat for animation, venv, and menu.
:: Logs written to: windows\logs\main_<timestamp>.log
:: ============================================================

set ROOT=%~dp0..
set "INSTANCE_ID=HASHI"
for /f "delims=" %%i in ('powershell -NoProfile -Command "$cfg = Get-Content '%ROOT%\agents.json' -Raw | ConvertFrom-Json; if ($cfg.global.instance_id) { $cfg.global.instance_id } else { 'HASHI' }"') do set "INSTANCE_ID=%%i"
set LOG_DIR=%~dp0logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=1-3 delims=/ " %%a in ("%DATE%") do set D=%%a-%%b-%%c
for /f "tokens=1-3 delims=:." %%a in ("%TIME: =0%") do set T=%%a%%b%%c
set LOG_FILE=%LOG_DIR%\main_%D%_%T%.log

echo ==================================================== >> "%LOG_FILE%"
echo %INSTANCE_ID% Main Bridge >> "%LOG_FILE%"
echo Started: %DATE% %TIME% >> "%LOG_FILE%"
echo ==================================================== >> "%LOG_FILE%"

cd /d "%ROOT%"

powershell -Command "& { $env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; cmd /c 'bin\bridge-u.bat --resume-last' 2>&1 | Tee-Object -FilePath '%LOG_FILE%' -Append }"
set EXIT_CODE=%ERRORLEVEL%

echo. >> "%LOG_FILE%"
echo Exit code: %EXIT_CODE% >> "%LOG_FILE%"
echo Stopped: %DATE% %TIME% >> "%LOG_FILE%"

echo.
echo Log saved to: %LOG_FILE%
pause
endlocal
