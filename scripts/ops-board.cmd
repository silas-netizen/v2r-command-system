@echo off
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
".venv\Scripts\python.exe" "scripts\ops_board.py" --slack >> "logs\ops-board.log" 2>&1
endlocal
