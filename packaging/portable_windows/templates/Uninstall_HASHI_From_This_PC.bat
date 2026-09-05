@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Uninstall-LocalCache.ps1"
if errorlevel 1 (
    echo.
    echo The HASHI runtime could not be fully removed.
    echo 无法完整删除 HASHI 本机运行组件。
    pause
    exit /b 1
)
echo.
echo Local HASHI runtime files were removed. USB data was not changed.
echo HASHI 本机运行组件已删除。USB 数据未被更改。
pause
