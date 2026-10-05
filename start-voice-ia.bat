@echo off
REM Foreground debug start (shows console logs)
setlocal
cd /d "%~dp0"
set "OLLAMA_CUDA=%LOCALAPPDATA%\Programs\Ollama\lib\ollama\cuda_v12"
if exist "%OLLAMA_CUDA%\cublas64_12.dll" set "PATH=%OLLAMA_CUDA%;%PATH%"
"%~dp0.venv\Scripts\python.exe" -u -m src.main %*
endlocal
