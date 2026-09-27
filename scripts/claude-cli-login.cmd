@echo off
chcp 65001 >nul
setlocal

REM Subscription-plan login - only needs to be done once.
REM When this window opens, type /login and press Enter, approve in the browser, done.
REM After that automation reuses this login. (Checked daily at 09:25.)

set "CLAUDE_EXE=%V2R_CLAUDE_EXE%"

if not defined CLAUDE_EXE (
  for /f "delims=" %%D in ('dir /b /o-n "%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code" 2^>nul') do (
    if not defined CLAUDE_EXE set "CLAUDE_EXE=%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\%%D\claude.exe"
  )
)

if not defined CLAUDE_EXE set "CLAUDE_EXE=claude"

echo.
echo ============================================================
echo  Claude Code login (subscription plan)
echo ============================================================
echo  Executable: %CLAUDE_EXE%
echo.
echo  1) The Claude Code screen will open shortly.
echo  2) Inside it, type  /login  and press Enter.
echo  3) When the browser opens, sign in and approve.
echo  4) When you see "Login successful", type  /exit  to close.
echo.
echo  * You only need to log in once; it stays valid.
echo ============================================================
echo.
pause

"%CLAUDE_EXE%"

echo.
echo You can close this window once the login check is done.
pause
endlocal
