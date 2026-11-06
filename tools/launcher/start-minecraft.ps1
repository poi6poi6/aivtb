$ErrorActionPreference = 'Stop'
$mcRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$mcServer = Join-Path $mcRoot 'data\minecraft\server'
if (-not (Test-Path -LiteralPath (Join-Path $mcServer 'eula.txt')) -or
    -not ((Get-Content -LiteralPath (Join-Path $mcServer 'eula.txt')) -contains 'eula=true')) {
    throw 'Please read https://www.minecraft.net/eula and set eula=true in data/minecraft/server/eula.txt if you agree.'
}
Start-Process -FilePath (Join-Path $mcRoot '.venv\Scripts\python.exe') `
    -ArgumentList @('-u', 'tools/minecraft_server.py') -WorkingDirectory $mcRoot `
    -WindowStyle Hidden -RedirectStandardOutput (Join-Path $mcServer 'wrapper.log') `
    -RedirectStandardError (Join-Path $mcServer 'wrapper-error.log')
Write-Host 'Server starting. Join 127.0.0.1:25565 after console.log says Done.'
