$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskDueUrl = 'http://127.0.0.1:8769'
try { $taskDueHealth = Invoke-RestMethod -Uri "$taskDueUrl/api/due-diligence/health" -TimeoutSec 2 } catch { $taskDueHealth = $null }
if ($taskDueHealth -and $taskDueHealth.app -ne 'due-diligence') { throw 'Port 8769 is occupied by another application.' }
if (-not $taskDueHealth) {
    $taskDuePython = Join-Path (Split-Path -Parent $PSScriptRoot) 'meeting-demo\.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath 'frontend-dist\due-diligence.html')) { throw 'Build the dedicated frontend first.' }
    $taskDueLog = Join-Path $PSScriptRoot 'data\logs'
    # Let the application initialize its owned data root before redirecting logs.
    if (-not (Test-Path -LiteralPath 'data\.due-app-data-root')) {
        & $taskDuePython -c 'from due_app.main import app; assert app.title'
        if ($LASTEXITCODE -ne 0) { throw 'The isolated data root could not be initialized.' }
    }
    Start-Process -FilePath $taskDuePython -ArgumentList @('-m','uvicorn','due_app.main:app','--host','127.0.0.1','--port','8769') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskDueLog 'server.stdout.log') -RedirectStandardError (Join-Path $taskDueLog 'server.stderr.log') | Out-Null
    for ($taskDueAttempt=0; $taskDueAttempt -lt 40; $taskDueAttempt++) {
        Start-Sleep -Milliseconds 250
        try { $taskDueHealth = Invoke-RestMethod -Uri "$taskDueUrl/api/due-diligence/health" -TimeoutSec 1 } catch { $taskDueHealth = $null }
        if ($taskDueHealth -and $taskDueHealth.app -eq 'due-diligence') { break }
    }
    if (-not $taskDueHealth -or $taskDueHealth.app -ne 'due-diligence') { throw 'The due-diligence service did not start. Inspect data/logs/server.stderr.log.' }
}
Start-Process "$taskDueUrl/due-diligence"
