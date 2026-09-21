@echo off
chcp 65001 > nul
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist logs mkdir logs
if "%~1"=="" (echo usage: web-login.cmd claude ^| make & pause & exit /b 2)
echo [V2R] A browser window will open. Please log in to %1 there (Google account). Waits up to 15 min.
".venv\Scripts\python.exe" -m v2r.warehouse.web_session %* > "logs\web-login.log" 2>&1
type "logs\web-login.log"
pause
