@echo off
chcp 65001 > nul
rem ChatGPT 로그인 창을 연다. 이 프로그램은 비밀번호를 입력하지도 저장하지도 않는다.
rem 열린 창에서 "직접" 로그인하면 세션이 data\browser-profile-gpt 에 저장된다.
rem 반드시 사용자가 로그인한 윈도우 데스크톱에서 실행할 것 (창이 떠야 한다).
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
call ".venv\Scripts\activate.bat"

echo.
echo  브라우저 창이 열립니다. 창에서 직접 ChatGPT에 로그인해 주세요 (최대 15분 대기).
echo.
if not exist logs mkdir logs
python -m v2r.warehouse.gpt_images --login > logs\gpt-login.log 2>&1
type logs\gpt-login.log
set RC=%errorlevel%

if "%RC%"=="0" (
  echo.
  echo  [완료] 로그인 세션이 저장되었습니다.
) else (
  echo.
  echo  [미완료] 로그인이 확인되지 않았습니다. 이 파일을 다시 실행해 주세요.
)
echo.
pause
endlocal
exit /b %RC%
