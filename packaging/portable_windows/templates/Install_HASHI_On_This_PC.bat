@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Install-LocalCache.ps1" -PauseOnError
if errorlevel 1 (
    echo.
    echo HASHI local acceleration was not installed. The USB copy is unchanged.
    pause
    exit /b 1
)
echo.
echo HASHI local acceleration is ready on this PC.
pause
