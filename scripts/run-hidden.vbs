' Run any .cmd hidden (no console window). Usage: wscript run-hidden.vbs <full path to .cmd> [args]
Set sh = CreateObject("WScript.Shell")
argsStr = ""
For i = 1 To WScript.Arguments.Count - 1
    argsStr = argsStr & " " & WScript.Arguments(i)
Next
sh.Run "cmd.exe /c """ & WScript.Arguments(0) & """" & argsStr, 0, False
