@echo off
chcp 65001 > nul
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist logs mkdir logs
echo [V2R] A browser window will open. Please log in to ChatGPT there (waits up to 15 min).
".venv\Scripts\python.exe" -m v2r.warehouse.gpt_images --login > logs\gpt-login.log 2>&1
type logs\gpt-login.log
pause