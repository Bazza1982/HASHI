@echo off
setlocal
set "stdin_probe="
set /p stdin_probe=
if defined stdin_probe (
    echo unexpected input 1>&2
    exit /b 47
)
echo fake stdout %*
echo fake stderr %* 1>&2
exit /b 0
