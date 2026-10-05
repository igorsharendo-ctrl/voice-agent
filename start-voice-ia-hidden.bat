@echo off
REM Silent background start: voice agent + status overlay (no console).

setlocal
cd /d "%~dp0"

set "OLLAMA_CUDA=%LOCALAPPDATA%\Programs\Ollama\lib\ollama\cuda_v12"
if exist "%OLLAMA_CUDA%\cublas64_12.dll" set "PATH=%OLLAMA_CUDA%;%PATH%"

set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
  echo [voice-ia] pythonw not found: %PYTHONW%
  exit /b 1
)

powershell -NoProfile -WindowStyle Hidden -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match '-m src\.(main|overlay)') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 400; Remove-Item '%~dp0outbox\*.pid' -Force -ErrorAction SilentlyContinue; $env:PATH = $env:PATH; Start-Process -FilePath '%PYTHONW%' -ArgumentList '-u','-m','src.main' -WorkingDirectory '%~dp0' -WindowStyle Hidden; Start-Process -FilePath '%PYTHONW%' -ArgumentList '-u','-m','src.overlay' -WorkingDirectory '%~dp0' -WindowStyle Hidden"

endlocal
exit /b 0
