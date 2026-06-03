@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy_win287_to_hetzner.ps1"
if errorlevel 1 exit /b %errorlevel%
powershell -NoProfile -ExecutionPolicy Bypass -File ".\run_win287_smoke_30m.ps1" -Seconds 1800
exit /b %errorlevel%
