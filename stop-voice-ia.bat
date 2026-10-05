@echo off
REM Stop ALL voice-ia agent + overlay processes (venv and system Python)
setlocal
cd /d "%~dp0"
powershell -NoProfile -Command ^
  "$n=0; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match '-m src\.(main|overlay)' -or $_.CommandLine -match 'voice-ia') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('stopped ' + $_.ProcessId); $n++ }; Remove-Item '%~dp0outbox\*.pid' -Force -ErrorAction SilentlyContinue; Write-Host ('done, killed~' + $n)"
endlocal
exit /b 0
