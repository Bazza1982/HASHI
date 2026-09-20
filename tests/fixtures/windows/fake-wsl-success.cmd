@echo off
echo fake stdout %*
echo fake stderr %* 1>&2
exit /b 0
