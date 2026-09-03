@echo off
:: Compatibility repair for an existing HASHI USB runtime.
:: The old embedded-Python ._pth patch is retired. Current packages use the
:: approved python-build-standalone CPython and a locked dependency generation.

setlocal
set "HASHI_ROOT=%~dp0"
set "PYTHON_DIR=%HASHI_ROOT%python"
set "PYTHON_VERSION=3.12.13"

if not exist "%PYTHON_DIR%\python.exe" (
    echo ERROR: python\python.exe not found relative to this script.
    echo        Place this script in the HASHI9 root folder and retry.
    pause
    exit /b 1
)

"%PYTHON_DIR%\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:3] == tuple(map(int, '%PYTHON_VERSION%'.split('.'))) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: This USB does not contain the approved CPython 3.12.13 runtime.
    echo        Rebuild it with windows\prepare_usb.bat; an in-place path patch
    echo        cannot migrate the HASHI Core runtime safely.
    pause
    exit /b 1
)

echo Repairing the approved dependency generation...
"%PYTHON_DIR%\python.exe" -m ensurepip --upgrade --default-pip >nul 2>&1
if errorlevel 1 goto :failed
"%PYTHON_DIR%\python.exe" -m pip install -r "%HASHI_ROOT%constraints\standard-py312.lock" --no-warn-script-location --quiet
if errorlevel 1 goto :failed

echo Validating the HASHI Core runtime contract...
"%PYTHON_DIR%\python.exe" "%HASHI_ROOT%scripts\check_runtime_contract.py"
if errorlevel 1 (
    goto :failed
) else (
    echo SUCCESS: the portable runtime satisfies the HASHI Core contract.
)
pause
endlocal
exit /b 0

:failed
echo FAILED: the portable runtime could not be repaired safely.
pause
endlocal
exit /b 1
