@echo off
:: PM2 Startup Script for Windows
:: Run this at system startup to restore PM2 processes and open browser

cd /d "%~dp0"

:: Start PM2 processes
pm2 resurrect

:: Wait for services to start (5 seconds)
timeout /t 5 /nobreak >nul

:: Open browser to Workbench
start "" "http://localhost:5173"
