' 브랜드 하나의 키워드 채우기 워커를 자기 숨김 콘솔로 띄운다(부모 cmd가 끝나도 살아남게).
' 사용: wscript.exe keyword-fill-worker.vbs <브랜드> [목표개수]
' 표준출력/오류는 logs\keyword-fill-<브랜드>.<시각>.out.log 로 띄울 때마다 새 파일에 모은다 — 다른 워커가
' 이미 >> 로 쥐고 있는 같은 파일에 리다이렉트하면 cmd가 열지 못해 워커가 소리 없이 exit 1로 죽는다(실측 2026-09-24 12:10).
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
brand = WScript.Arguments(0)
target = "10000"
stamp = Replace(CStr(Timer), ".", "")
If WScript.Arguments.Count > 1 Then target = WScript.Arguments(1)
cmd = "cmd.exe /c chcp 65001 > nul & cd /d """ & root & """ & set ""PYTHONUTF8=1"" & set ""PYTHONIOENCODING=utf-8"" & set ""PLAYWRIGHT_BROWSERS_PATH=" & root & "\.pw-browsers"" & "".venv\Scripts\python.exe"" -m v2r.knowledge.keyword_fill_loop --worker " & brand & " " & target & " >> ""logs\keyword-fill-" & brand & "." & stamp & ".out.log"" 2>&1"
sh.Run cmd, 0, False
