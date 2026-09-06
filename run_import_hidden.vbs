' run_import_hidden.vbs - launch the importer with no visible window.
'
' A .bat run by Task Scheduler pops a console window that takes focus, which
' is disruptive over a fullscreen game. This wrapper runs the same batch file
' with window style 0 (hidden), so nothing ever appears on screen.
'
' Point the scheduled task at this instead of the .bat:
'
'   schtasks /create /tn "WoW AH import" /sc hourly /f ^
'     /tr "wscript.exe \"C:\Dev\Wow-ah\wowah\run_import_hidden.vbs\""
'
' Output still goes to import.log - that is the only place to check on it.

Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

' 0 = hidden window, False = don't wait for it to finish
shell.Run "cmd /c """ & here & "run_import.bat""", 0, False
