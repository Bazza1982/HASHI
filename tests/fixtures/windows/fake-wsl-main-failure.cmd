@echo off
echo %* | %SystemRoot%\System32\findstr.exe /C:"/usr/bin/test" >nul
if not errorlevel 1 exit /b 0
echo fake main failure 1>&2
exit /b 23
