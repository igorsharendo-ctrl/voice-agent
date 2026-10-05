@echo off
REM Enroll personal voiceprints for «ИА» and «прием».
REM Stop the agent first if it is holding the mic.

setlocal
cd /d "%~dp0"

set "OLLAMA_CUDA=%LOCALAPPDATA%\Programs\Ollama\lib\ollama\cuda_v12"
if exist "%OLLAMA_CUDA%\cublas64_12.dll" set "PATH=%OLLAMA_CUDA%;%PATH%"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [voice-ia] python not found: %PY%
  exit /b 1
)

echo Stopping agent if running...
call "%~dp0stop-voice-ia.bat" >nul 2>&1

REM Device 8 = UGREEN from config.yaml — change if needed
"%PY%" -m src.calibrate --device 8 --count 5
if errorlevel 1 (
  echo Calibration failed.
  pause
  exit /b 1
)

echo.
echo Restart with start-voice-ia-hidden.bat when ready.
pause
endlocal
exit /b 0
