@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Uninstall-LocalCache.ps1"
if errorlevel 1 (
    echo.
    echo HASHI local acceleration could not be fully removed.
    pause
    exit /b 1
)
echo.
echo Local program files were removed. USB data was not changed.
pause
