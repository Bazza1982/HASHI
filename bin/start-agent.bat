@echo off
if "%~1"=="" (
  echo Usage: start-agent.bat agent-name
  exit /b 1
)
curl -s -X POST http://127.0.0.1:18800/api/admin/start-agent -H "Content-Type: application/json" -d "{\"agent\":\"%~1\"}"

