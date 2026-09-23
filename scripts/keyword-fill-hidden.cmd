@echo off
chcp 65001 > nul
rem 브랜드 5개(우아덤·코숨핏·뉴더미스·장으뜸·팥순이) 키워드 채우기를 각각
rem 별도 숨김 프로세스로 띄운다. 로그: logs\keyword-fill-<브랜드>.log
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"

echo [%date% %time%] keyword-fill start >> "logs\keyword-fill.log"

start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_fill_loop --worker 우아덤 10000 >> "logs\keyword-fill-우아덤.out.log" 2>&1
start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_fill_loop --worker 코숨핏 10000 >> "logs\keyword-fill-코숨핏.out.log" 2>&1
start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_fill_loop --worker 뉴더미스 10000 >> "logs\keyword-fill-뉴더미스.out.log" 2>&1
start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_fill_loop --worker 장으뜸 10000 >> "logs\keyword-fill-장으뜸.out.log" 2>&1
start "" /B ".venv\Scripts\python.exe" -m v2r.knowledge.keyword_fill_loop --worker 팥순이 10000 >> "logs\keyword-fill-팥순이.out.log" 2>&1

echo [%date% %time%] keyword-fill 5 workers launched >> "logs\keyword-fill.log"
