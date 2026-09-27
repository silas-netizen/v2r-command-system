@echo off
rem V2R unified supervisor. Runs scripts\supervisor.py (stdlib only).
rem supervisor.py writes its own one-line summary to logs\supervisor.log
rem (do not also redirect stdout there - two handles on the same file at
rem once can silently drop writes on Windows). Uncaught errors go here instead.
rem State: data\supervisor_last.json  Error log: logs\supervisor-error.log
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" "scripts\supervisor.py" >> "logs\supervisor-error.log" 2>&1
endlocal
