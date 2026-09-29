' Run keep_awake.py hidden (no window). Scheduled task V2R-KeepAwake (at logon) + supervisor restart.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run """" & root & "\.venv\Scripts\pythonw.exe"" """ & root & "\scripts\keep_awake.py""", 0, False
