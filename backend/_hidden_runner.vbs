' Generic hidden launcher: run a .bat/.cmd in a fully hidden console,
' then hand the script's exit code back to the calling Scheduled Task.
' Usage: wscript.exe _hidden_runner.vbs "<path-to-bat>"
' Path must stay pure ASCII: WSH reads .vbs as ANSI.
Option Explicit

Dim fso, sh, bat, rc
Set fso = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count = 0 Then WScript.Quit 1
bat = WScript.Arguments(0)
If Not fso.FileExists(bat) Then WScript.Quit 2

Set sh = CreateObject("WScript.Shell")
' 0 = hidden window, True = wait for completion so the task keeps its
' original "wait for the script, record its result code" semantics.
rc = sh.Run("cmd /c """ & bat & """", 0, True)
WScript.Quit rc