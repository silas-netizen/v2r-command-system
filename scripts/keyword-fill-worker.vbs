' 브랜드 하나의 키워드 채우기 워커를 자기 숨김 콘솔로 띄운다(부모 cmd가 끝나도 살아남게).
' 사용: wscript.exe keyword-fill-worker.vbs <브랜드> [목표개수]
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
brand = WScript.Arguments(0)
target = "10000"
If WScript.Arguments.Count > 1 Then target = WScript.Arguments(1)
cmd = "cmd.exe /c chcp 65001 > nul & cd /d """ & root & """ & set PYTHONUTF8=1 & set PYTHONIOENCODING=utf-8 & set PLAYWRIGHT_BROWSERS_PATH=" & root & "\.pw-browsers & "".venv\Scripts\python.exe"" -m v2r.knowledge.keyword_fill_loop --worker " & brand & " " & target & " >> ""logs\keyword-fill-" & brand & ".out.log"" 2>&1"
sh.Run cmd, 0, False
