@echo off
chcp 65001 > nul
rem V2R health check (backup safety net). Restarts the runner if it stopped.
rem Register once from an admin prompt (every 5 minutes):
rem   schtasks /create /tn "V2R-Health" /tr "\"D:\v2r automation\v2r-command-system\scripts\health-check.cmd\"" /sc minute /mo 5 /f
rem Log: logs\health.log
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
call ".venv\Scripts\activate.bat"

echo [%date% %time%] --- health check --- >> "logs\health.log"
python -m v2r health >> "logs\health.log" 2>&1

rem exit code 1 if the heartbeat is older than 3 minutes
python -c "from v2r.engine.context import Runtime; from v2r.engine.schedule import heartbeat_is_stale; rt=Runtime.open(); import sys; bad=heartbeat_is_stale(rt,180); rt.close(); sys.exit(1 if bad else 0)"
if errorlevel 1 (
  echo [%date% %time%] heartbeat missing/stale - restarting V2R-Serve >> "logs\health.log"
  schtasks /run /tn "V2R-Serve" >> "logs\health.log" 2>&1
) else (
  echo [%date% %time%] runner OK >> "logs\health.log"
)
endlocal
