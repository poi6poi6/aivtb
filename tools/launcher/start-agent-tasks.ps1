$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$taskData = Join-Path $taskRoot 'data\agent-tasks'
New-Item -ItemType Directory -Force -Path $taskData | Out-Null
if (Get-NetTCPConnection -LocalPort 8085 -State Listen -ErrorAction SilentlyContinue) {
    try {
        $taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8085/task-service-health' -TimeoutSec 2
        if ($taskHealth.service -eq 'avatar-tasks' -and $taskHealth.ready) {
            Write-Host 'Task service already running: http://127.0.0.1:8085/tasks'
            exit 0
        }
    } catch {}
    throw 'Port 8085 is occupied by an older task service or another program. Stop it before upgrading.'
}
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
Start-Process -FilePath $taskPython -ArgumentList @('-u','tools/run_agent_console.py') -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskData 'console.log') -RedirectStandardError (Join-Path $taskData 'console-error.log') | Out-Null
Write-Host 'Independent task service starting: http://127.0.0.1:8085/tasks'
Write-Host 'Closing the main conversation does not stop background tasks. Use the main launcher menu or tools\launcher\stop-agent-tasks.ps1 to stop this service.'
