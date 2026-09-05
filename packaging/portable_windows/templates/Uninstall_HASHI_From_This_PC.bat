@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Uninstall-LocalCache.ps1"
set "HASHI_UNINSTALL_EXIT=%ERRORLEVEL%"
if "%HASHI_UNINSTALL_EXIT%"=="2" (
    echo.
    echo Removal cancelled. Nothing was changed.
    echo 已取消删除，未作任何更改。
    pause
    exit /b 0
)
if not "%HASHI_UNINSTALL_EXIT%"=="0" (
    echo.
    echo The HASHI runtime could not be fully removed.
    echo 无法完整删除 HASHI 本机运行组件。
    pause
    exit /b 1
)
echo.
echo This HASHI Portable instance was removed from the PC. USB data was not changed.
echo 此 HASHI 便携实例已从本机删除。USB 数据未被更改。
pause
