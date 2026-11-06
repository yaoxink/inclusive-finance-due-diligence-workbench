$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
# ASR model keep-alive: parked workers reuse the loaded decoder, so a new
# recording segment no longer pays the ~50s model load on every start.
$env:DUE_ASR_KEEPALIVE = '1'
$python = Join-Path (Split-Path -Parent $PSScriptRoot) 'meeting-demo\.venv\Scripts\python.exe'
& $python -m uvicorn due_app.main:app --host 127.0.0.1 --port 8769
