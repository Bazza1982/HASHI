@echo off
:: Quick fix: patch the Python ._pth file on USB to include project root
:: Run this from the USB (D:\HASHI9\) or any location — it auto-detects.

setlocal
set "PYTHON_DIR=%~dp0python"

if not exist "%PYTHON_DIR%\python.exe" (
    echo ERROR: python\python.exe not found relative to this script.
    echo        Place this script in the HASHI9 root folder and retry.
    pause
    exit /b 1
)

set "PTH_FILE="
for %%f in ("%PYTHON_DIR%\python*._pth") do set "PTH_FILE=%%f"

if "%PTH_FILE%"=="" (
    echo ERROR: No ._pth file found in %PYTHON_DIR%
    pause
    exit /b 1
)

echo Patching %PTH_FILE% ...
(
    echo python313.zip
    echo .
    echo ..
    echo import site
) > "%PTH_FILE%"

echo Done. Testing import...
"%PYTHON_DIR%\python.exe" -c "import orchestrator; print('OK: orchestrator found at', orchestrator.__file__)"
if errorlevel 1 (
    echo FAILED: orchestrator still not importable.
) else (
    echo SUCCESS: Python can now find all HASHI modules.
)
pause
endlocal
