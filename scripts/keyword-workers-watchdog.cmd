@echo off
chcp 65001 > nul
rem 키워드 채점(score) 5개 + 2차 판정(codex) 10개 워커가 살아 있는지 확인하고, 모자라면 다시 띄운다.
rem 예약 작업 V2R-KeywordWorkers(5분 간격)가 keyword-workers-watchdog.vbs 로 숨김 실행한다.
rem 2026-09-26 재발 방지: 어제 저녁 워커가 모두 종료된 채 후보 27,592개가 미채점으로 쌓였다.
rem 워커 자체도 상주형으로 바꿨고(일이 없으면 60초 쉼, 오류는 60초 뒤 재진입), 이 감시가 2중 안전장치다.
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"

rem 정지 파일이 있으면 아무것도 하지 않는다(사용자가 일부러 세운 상태)
if exist "data\STOP" (
    echo [%date% %time%] keyword-watchdog: 정지 파일 있음, 조치 없음 >> "logs\keyword-workers-watchdog.log"
    exit /b 0
)

for /f %%N in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { $_.CommandLine -like '*keyword_relevance*' }).Count"') do set ALIVE=%%N
if "%ALIVE%"=="" set ALIVE=0

if %ALIVE% GEQ 15 (
    echo [%date% %time%] keyword-watchdog: 워커 %ALIVE%개 실행 중, 조치 없음 >> "logs\keyword-workers-watchdog.log"
) else (
    echo [%date% %time%] keyword-watchdog: 워커 %ALIVE%개(15 기대) - rescore.cmd 로 보충 >> "logs\keyword-workers-watchdog.log"
    rem rescore.cmd 는 브랜드별 잠금 파일(data\locks\score-*, codex-*)로 이미 도는 워커를 건너뛰므로 모자란 것만 뜬다
    cscript //nologo "%~dp0rescore-hidden.vbs" >> "logs\keyword-workers-watchdog.log" 2>&1
)
