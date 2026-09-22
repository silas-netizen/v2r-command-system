@echo off
chcp 65001 > nul
rem V2R 실행기(텔레그램 명령 수신). 죽으면 30초 뒤 다시 켠다.
rem 로그: logs\serve.log  /  멈추려면 이 창을 닫거나 작업 스케줄러에서 V2R-Serve 종료
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
rem 2026-09-23: Claude 앱(MSIX) 셸에서 설치한 AppData\Local\ms-playwright 는 패키지 가상 폴더라
rem 실행기(일반 프로세스)가 못 본다 -> 저장소 안 .pw-browsers 를 쓴다 (playwright install 도 이 경로로).
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"

:loop
echo [%date% %time%] serve start >> "logs\serve.log"
rem 2026-09-23: activate 대신 venv python을 직접 부른다
".venv\Scripts\python.exe" -m v2r serve >> "logs\serve.log" 2>&1
echo [%date% %time%] serve stopped (exit %errorlevel%), restarting in 30s >> "logs\serve.log"
timeout /t 30 /nobreak > nul
goto loop
