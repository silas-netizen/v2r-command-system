@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
call ".venv\Scripts\activate.bat"
rem Non-ASCII arguments break in cmd, so the command text is read from a file instead.
python scripts\enqueue.py --file scripts\daily-0900.txt >> "logs\enqueue-0900.log" 2>&1
