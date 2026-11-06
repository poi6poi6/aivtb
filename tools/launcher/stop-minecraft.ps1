$ErrorActionPreference = 'Stop'
$mcRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$mcServer = Join-Path $mcRoot 'data\minecraft\server'
if (Test-Path -LiteralPath $mcServer) {
    Set-Content -LiteralPath (Join-Path $mcServer 'stop.request') -Value 'stop' -Encoding ascii
    Write-Host 'Graceful stop requested. Check console.log for world saved before closing Windows.'
}
