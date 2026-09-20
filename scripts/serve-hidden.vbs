' V2R 실행기를 창 없이 띄운다 (예약 작업 V2R-Serve가 이 파일을 실행).
' 로그는 logs\serve.log, 상태는 python -m v2r health / 텔레그램 '현황'.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run "cmd.exe /c """ & root & "\scripts\serve.cmd""", 0, False
