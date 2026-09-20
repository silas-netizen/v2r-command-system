@echo off
chcp 65001 > nul
rem V2R 건강 점검(백업 안전망). 실행기가 멈췄으면 다시 켠다.
rem 등록(관리자 명령 프롬프트에서 한 번만, 5분마다):
rem   schtasks /create /tn "V2R-Health" /tr "\"D:\v2r 자동화\v2r-command-system\scripts\health-check.cmd\"" /sc minute /mo 5 /f
rem 기록: logs\health.log
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
call ".venv\Scripts\activate.bat"

echo [%date% %time%] --- health check --- >> "logs\health.log"
python -m v2r health >> "logs\health.log" 2>&1

rem 심장박동이 3분보다 오래됐으면 종료 코드 1
python -c "from v2r.engine.context import Runtime; from v2r.engine.schedule import heartbeat_is_stale; rt=Runtime.open(); import sys; bad=heartbeat_is_stale(rt,180); rt.close(); sys.exit(1 if bad else 0)"
if errorlevel 1 (
  echo [%date% %time%] 심장박동 없음/오래됨 - V2R-Serve 다시 시작 >> "logs\health.log"
  schtasks /run /tn "V2R-Serve" >> "logs\health.log" 2>&1
) else (
  echo [%date% %time%] 실행기 정상 >> "logs\health.log"
)
endlocal
