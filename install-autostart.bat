@echo off
REM Create Startup shortcut for silent Voice-IA agent
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%~dp0start-voice-ia-hidden.bat"
set "LINK=%STARTUP%\Voice-IA.lnk"

powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%LINK%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Description = 'Local voice agent IA'; $s.Save(); Write-Host 'Installed:' '%LINK%'"

echo.
echo Shortcut created. Agent will start at Windows logon (no console).
echo Remove the shortcut from Startup folder to disable.
endlocal
pause
