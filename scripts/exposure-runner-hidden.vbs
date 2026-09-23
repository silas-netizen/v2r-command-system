' 노출 확인 러너(상주 브라우저 작업자)를 창 없이 띄운다.
' 로그: logs\exposure-runner-<n>.log, 진행: data\exposure_runner_state.json
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run "cmd.exe /c """ & root & "\scripts\exposure-runner-hidden.cmd""", 0, False
