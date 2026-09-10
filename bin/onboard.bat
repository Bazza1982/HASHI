@echo off
setlocal
node "%~dp0..\onboard-cli.js" %*
exit /b %errorlevel%
