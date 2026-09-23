' 노출 확인 러너(상주 브라우저 작업자)를 창 없이 띄운다.
' 로그: logs\exposure-runner-<n>.log, 진행: data\exposure_runner_state.json
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

' 2026-09-24 수정 — 이전엔 cscript에 준 인자(작업자 수)를 .cmd로 안 넘겨서
' 항상 기본값(2)으로만 떴다. WScript.Arguments를 그대로 붙여 전달한다.
argsStr = ""
For i = 0 To WScript.Arguments.Count - 1
    argsStr = argsStr & " " & WScript.Arguments(i)
Next

sh.Run "cmd.exe /c """ & root & "\scripts\exposure-runner-hidden.cmd""" & argsStr, 0, False
