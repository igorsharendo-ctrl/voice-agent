$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$ollamaCuda = Join-Path $env:LOCALAPPDATA "Programs\Ollama\lib\ollama\cuda_v12"
if (Test-Path $ollamaCuda) {
    $env:PATH = "$ollamaCuda;$env:PATH"
}

& "$PSScriptRoot\.venv\Scripts\python.exe" -u -m src.main @args
