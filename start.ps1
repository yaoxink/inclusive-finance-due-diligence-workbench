$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskDuePython = Join-Path (Split-Path -Parent $PSScriptRoot) 'meeting-demo\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskDuePython)) { throw 'The shared Python runtime is missing. Set up meeting-demo first.' }
if (-not (Test-Path -LiteralPath 'frontend-dist\due-diligence.html')) { throw 'Build the dedicated due-diligence frontend first.' }
Write-Host 'Open http://127.0.0.1:8769/due-diligence . Press Ctrl+C to stop this instance.'
& $taskDuePython -m uvicorn due_app.main:app --host 127.0.0.1 --port 8769
