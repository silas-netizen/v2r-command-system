' 브랜드 5개 연관도 재채점(당위성/무관 분리 + GPT 교차검증)을 창 없이 동시에 띄운다.
' 로그: logs\rescore-<브랜드>.log, 진행: data\keywords\rescore_progress_<브랜드>.json
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run "cmd.exe /c """ & root & "\scripts\rescore.cmd""", 0, False
