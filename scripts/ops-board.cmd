@echo off
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv\Scripts\python.exe" "scripts\ops_board.py" --slack >> "logs\ops-board.log" 2>&1
endlocal
