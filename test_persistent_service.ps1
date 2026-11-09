$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'Chanxu-Local-8765' -ErrorAction SilentlyContinue
if (-not $task) { throw 'FAIL: no independent Windows scheduled task' }
if ($task.Settings.ExecutionTimeLimit -ne 'PT0S') { throw 'FAIL: task execution is time limited' }
$h = Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 5
if ($h.service -ne 'chanxu-local-data') { throw 'FAIL: wrong or unavailable service' }
Write-Output 'PASS: independent task exists, no execution time limit, correct service healthy'
