@echo off
chcp 65001 >nul
setlocal

REM 요금제(구독) 길 로그인 — 한 번만 하면 됩니다.
REM 이 창이 열리면 /login 이라고 치고 엔터, 브라우저에서 승인하면 끝납니다.
REM 그 뒤로는 자동화가 알아서 그 로그인을 씁니다. (점검 예약: 매일 09:25)

set "CLAUDE_EXE=%V2R_CLAUDE_EXE%"

if not defined CLAUDE_EXE (
  for /f "delims=" %%D in ('dir /b /o-n "%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code" 2^>nul') do (
    if not defined CLAUDE_EXE set "CLAUDE_EXE=%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\%%D\claude.exe"
  )
)

if not defined CLAUDE_EXE set "CLAUDE_EXE=claude"

echo.
echo ============================================================
echo  Claude Code 로그인 (요금제 길)
echo ============================================================
echo  실행 파일: %CLAUDE_EXE%
echo.
echo  1) 잠시 뒤 클로드 코드 화면이 열립니다.
echo  2) 그 안에서  /login  이라고 치고 엔터를 누르세요.
echo  3) 브라우저가 열리면 계정으로 로그인하고 승인하세요.
echo  4) "Login successful" 이 보이면  /exit  로 닫으면 됩니다.
echo.
echo  * 로그인은 한 번만 하면 계속 유지됩니다.
echo ============================================================
echo.
pause

"%CLAUDE_EXE%"

echo.
echo 로그인 확인이 끝났으면 이 창을 닫아도 됩니다.
pause
endlocal
