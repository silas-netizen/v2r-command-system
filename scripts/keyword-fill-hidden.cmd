@echo off
chcp 65001 > nul
rem 브랜드 5개(우아덤·코숨핏·뉴더미스·장으뜸·팥순이) 키워드 채우기를 각각
rem 별도 숨김 콘솔(scripts\keyword-fill-worker.vbs)로 띄운다 — 이 cmd가 끝나도 워커는 남는다.
rem 로그: logs\keyword-fill-<브랜드>.log
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.pw-browsers
if not exist "logs" mkdir "logs"

echo [%date% %time%] keyword-fill start >> "logs\keyword-fill.log"


wscript.exe "scripts\keyword-fill-worker.vbs" 우아덤 10000
wscript.exe "scripts\keyword-fill-worker.vbs" 코숨핏 10000
wscript.exe "scripts\keyword-fill-worker.vbs" 뉴더미스 10000
wscript.exe "scripts\keyword-fill-worker.vbs" 장으뜸 10000
wscript.exe "scripts\keyword-fill-worker.vbs" 팥순이 10000
echo [%date% %time%] keyword-fill 5 workers launched >> "logs\keyword-fill.log"
