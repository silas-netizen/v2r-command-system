@echo off
chcp 65001 > nul
rem 끊긴 작업 점검(하루 1번). 결과는 텔레그램으로도 보고된다.
rem 로그: logs\reconcile.log
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
call ".venv\Scripts\activate.bat"

echo [%date% %time%] reconcile start >> "logs\reconcile.log"
python -m v2r "끊긴 작업 점검" >> "logs\reconcile.log" 2>&1
echo [%date% %time%] reconcile done (exit %errorlevel%) >> "logs\reconcile.log"
