' 브랜드 5개 키워드 채우기(원고 대상 1만 개까지 순환)를 창 없이 띄운다.
' 로그: logs\keyword-fill-<브랜드>.log, 진행: data\keywords\fill_progress.json
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run "cmd.exe /c """ & root & "\scripts\keyword-fill-hidden.cmd""", 0, False
