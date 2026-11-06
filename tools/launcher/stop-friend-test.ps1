$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$recordFile = Join-Path $taskRoot 'data/friend-test/processes.json'
$records = Get-Content -LiteralPath $recordFile -Raw | ConvertFrom-Json
foreach ($record in $records) {
    $taskProcess = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($taskProcess -and [math]::Abs(($taskProcess.StartTime.ToUniversalTime() - ([datetime]$record.started).ToUniversalTime()).TotalMilliseconds) -lt 1) {
        # Verify the creation time before stopping this task's process tree.
        & taskkill.exe /PID $record.id /T /F
    }
}
Write-Output 'Friend test stopped. Daily service is unchanged.'
