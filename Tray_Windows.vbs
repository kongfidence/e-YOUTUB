' ============================================================
'  Universal Downloader  ::  TRAY version  ::  WINDOWS ONLY
'  ------------------------------------------------------------
'  Double-click to start. NO console window is shown.
'  A red icon appears in the system tray (bottom-right).
'  Right-click the tray icon -> "退出" to quit; quitting the
'  tray AUTO-CLOSES the app. Nothing stays running afterwards.
' ============================================================
'  (Tray version is Windows only. On Mac use Browser_Mac.command)

Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' --- Check Python ---
ret = WshShell.Run("cmd /c python --version >nul 2>&1", 0, True)
If ret <> 0 Then
    MsgBox "Python not found." & vbCrLf & vbCrLf & _
           "Install Python 3.8+ from https://www.python.org/downloads/" & vbCrLf & _
           "(tick 'Add Python to PATH' during install).", _
           vbCritical, "Universal Downloader"
    WScript.Quit
End If

' --- Install dependencies silently (no-op if already present) ---
WshShell.Run "cmd /c pip install pystray Pillow yt-dlp " & _
             "--quiet --disable-pip-version-check 2>nul", 0, True

' --- Temp launcher .py to avoid Unicode-path issues on the command line ---
tempDir = WshShell.ExpandEnvironmentStrings("%TEMP%")
launcherPath = tempDir & "\ud_tray_launcher.py"
Set ts = fso.CreateTextFile(launcherPath, True, False)
ts.WriteLine "import os, sys"
ts.WriteLine "if sys.stdout is None: sys.stdout = open(os.devnull, 'w', encoding='utf-8')"
ts.WriteLine "if sys.stderr is None: sys.stderr = open(os.devnull, 'w', encoding='utf-8')"
ts.WriteLine "target = r'" & scriptDir & "'"
ts.WriteLine "os.chdir(target)"
ts.WriteLine "sys.path.insert(0, target)"
ts.WriteLine "__file__ = os.path.join(target, 'yt_tray.pyw')"
ts.WriteLine "with open(__file__, encoding='utf-8') as f:"
ts.WriteLine "    exec(compile(f.read(), __file__, 'exec'), {'__name__': '__main__', '__file__': __file__})"
ts.Close

' --- Launch hidden with pythonw (NO console window) ---
WshShell.Run "pythonw """ & launcherPath & """", 0, False
