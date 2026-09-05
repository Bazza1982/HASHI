@echo off
chcp 65001 >nul
setlocal
:install
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\Install-LocalCache.ps1"
if errorlevel 1 (
    echo.
    echo HASHI Setup did not complete. No personal data was moved or deleted.
    echo HASHI 安装未完成。个人数据没有被移动或删除。
    echo.
    echo Setup log: %~dp0data\logs\hashi-setup.log
    echo 安装日志：%~dp0data\logs\hashi-setup.log
    echo.
    choice /c RX /n /m "[R] Retry / 重试    [X] Exit / 退出: "
    if errorlevel 2 exit /b 1
    goto install
)
echo.
echo HASHI is ready on this PC.
echo HASHI 已在这台电脑上准备就绪。
pause
