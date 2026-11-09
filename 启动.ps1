param([ValidateRange(1024,65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = (Get-Command python -ErrorAction Stop).Source
$PythonW = Join-Path (Split-Path $Python) 'pythonw.exe'
if (Test-Path -LiteralPath $PythonW) { $Python = $PythonW }
$TaskName = "Chanxu-Local-$Port"
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}" --port {1}' -f (Join-Path $Root 'supervisor.py'), $Port) -WorkingDirectory $Root
$Login = New-ScheduledTaskTrigger -AtLogOn -User $User
$Retry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Task) {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($Login,$Retry) -Settings $Settings -Principal $Principal -Description 'Chanxu localhost watchdog; current user login; no saved password.' | Out-Null
} elseif ($Task.Actions.Arguments -notlike ('*'+(Join-Path $Root 'supervisor.py')+'*')) {
    throw "Existing task $TaskName belongs to a different path; refusing overwrite."
}
Start-ScheduledTask -TaskName $TaskName
$Ready = $false
for ($i=0; $i -lt 30; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($h.ok -and $h.service -eq 'chanxu-local-data') { $Ready=$true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $Ready) { throw "Service not ready. Check server-error.log and supervisor-$Port.log in $Root" }
Write-Output "缠序已就绪：http://127.0.0.1:$Port/"
Write-Output "Windows 托管任务：$TaskName；登录自启、退出恢复；不依赖聊天窗口。"
