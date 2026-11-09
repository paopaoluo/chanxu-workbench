@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0启动.ps1"
if errorlevel 1 (
  echo Startup failed. See the error above.
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8765/"
