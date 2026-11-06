$ErrorActionPreference = 'Stop'
try { $taskDueHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8769/api/due-diligence/health' -TimeoutSec 2 } catch { Write-Host 'The due-diligence service is not running.'; exit 0 }
if ($taskDueHealth.app -ne 'due-diligence') { throw 'The service identity does not match. No process was stopped.' }
if ($taskDueHealth.active_recording_id) { throw 'Recording is active. Stop it in the workbench first.' }
if ($taskDueHealth.asr_busy_count -gt 0) { throw 'Transcription is still finishing. Wait for completion before stopping.' }
$taskDuePid = [int]$taskDueHealth.pid
$taskDueProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $taskDuePid"
if (-not $taskDueProcess -or $taskDueProcess.CommandLine -notlike '*due_app.main:app*') { throw 'Process identity could not be verified. No process was stopped.' }
Stop-Process -Id $taskDuePid -ErrorAction Stop
Write-Host 'The due-diligence service was stopped.'
