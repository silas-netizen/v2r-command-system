' 브랜드 5개 연관도 채점(클로드 score-worker 1개 + Codex codex-worker 2개, 총 15개
' 프로세스)을 창 없이 동시에 띄운다. 2026-09-25 분리 구조.
' 로그: logs\score-<브랜드>.out.log, logs\codex-<브랜드>-<n>.out.log
' 진행: data\keywords\score_progress_<브랜드>.json, codex_progress_<브랜드>_<n>.json
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run "cmd.exe /c """ & root & "\scripts\rescore.cmd""", 0, False
