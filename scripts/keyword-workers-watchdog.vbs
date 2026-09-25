' 키워드 워커 감시를 창 없이 실행한다(예약 작업 V2R-KeywordWorkers).
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run "cmd.exe /c """ & root & "\scripts\keyword-workers-watchdog.cmd""", 0, False
