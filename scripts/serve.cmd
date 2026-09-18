@echo off
chcp 65001 > nul
rem V2R 실행기(텔레그램 명령 수신). 죽으면 30초 뒤 다시 켠다.
rem 로그: logs\serve.log  /  멈추려면 이 창을 닫거나 작업 스케줄러에서 V2R-Serve 종료
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
call ".venv\Scripts\activate.bat"

:loop
echo [%date% %time%] serve start >> "logs\serve.log"
python -m v2r serve >> "logs\serve.log" 2>&1
echo [%date% %time%] serve stopped (exit %errorlevel%), restarting in 30s >> "logs\serve.log"
timeout /t 30 /nobreak > nul
goto loop
