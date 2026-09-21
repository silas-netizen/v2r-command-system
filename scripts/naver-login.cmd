@echo off
chcp 65001 > nul
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist logs mkdir logs
echo [V2R] A browser window will open. Please log in to NAVER there and check "Keep me logged in" (waits up to 15 min).
".venv\Scripts\python.exe" -m v2r.warehouse.naver_session --login > "logs\naver-login.log" 2>&1
type "logs\naver-login.log"
pause
