$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$taskData = Join-Path $taskRoot 'data\agent-tasks'
if (Test-Path -LiteralPath $taskData) {
    Set-Content -LiteralPath (Join-Path $taskData 'stop.request') -Value 'stop' -Encoding ascii
    Write-Host 'Requested task cancellation and console shutdown.'
}
