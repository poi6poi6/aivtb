$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskRuntime = Join-Path $taskRoot 'data\agent-runtime'
New-Item -ItemType Directory -Force -Path $taskRuntime | Out-Null
$taskBinary = Join-Path $taskRuntime 'omp.exe'
if (Test-Path -LiteralPath $taskBinary) {
    & $taskBinary --version
    exit 0
}
Invoke-WebRequest 'https://github.com/can1357/oh-my-pi/releases/download/v18.1.17/omp-windows-x64.exe' -OutFile $taskBinary
& $taskBinary --version
