Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd /c """ & scriptDir & "\clocktower.bat""""
shell.Run cmd, 0, False
