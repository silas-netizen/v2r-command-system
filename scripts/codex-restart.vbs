' codex-worker 10개 재기동(창 없이). scripts\codex-restart.cmd 참고.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run "cmd.exe /c """ & root & "\scripts\codex-restart.cmd""", 0, False
