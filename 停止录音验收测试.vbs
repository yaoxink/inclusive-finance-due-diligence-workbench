Option Explicit
Dim fso, appRoot, dataRoot, marker, handle
Set fso = CreateObject("Scripting.FileSystemObject")
appRoot = fso.GetParentFolderName(WScript.ScriptFullName)
dataRoot = fso.BuildPath(appRoot, "data")
If Not fso.FolderExists(dataRoot) Then fso.CreateFolder(dataRoot)
marker = fso.BuildPath(dataRoot, "live-recording-test.stop")
Set handle = fso.CreateTextFile(marker, True, True)
handle.WriteLine "user_stop_requested " & Now
handle.Close
MsgBox "Stop requested. The recording acceptance driver will save and stop shortly.", 64, "Recording acceptance"
