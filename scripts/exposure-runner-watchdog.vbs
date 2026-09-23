' 노출 확인 러너 생존 확인 + 죽었으면 재기동(창 없이). 예약 작업
' V2R-ExposureRunner(로그온 시 + 5분마다)가 이 파일을 부른다.
' 로그: logs\exposure-runner-watchdog.log
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run "cmd.exe /c """ & root & "\scripts\exposure-runner-watchdog.cmd""", 0, False
