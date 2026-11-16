@echo off
rem HASHI vision skill launcher: v <image...> [options]
rem Uses `python` from PATH, falls back to the Windows py launcher.
setlocal
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python -B "%~dp0vision.py" %*
) else (
    py -3 -B "%~dp0vision.py" %*
)
exit /b %ERRORLEVEL%
