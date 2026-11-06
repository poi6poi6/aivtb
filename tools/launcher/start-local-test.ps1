param(
    [ValidateRange(1024, 65535)] [int]$Port = 18080,
    [switch]$NoBrowser,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$testRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$testPython = Join-Path $testRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $testPython)) {
    Write-Error 'Project Python is missing. Set up the project .venv before running the local test.'
    exit 1
}
$testArguments = @('-m', 'brain.local_test', '--port', "$Port")
if ($NoBrowser) { $testArguments += '--no-browser' }
if ($ValidateOnly) { $testArguments += '--validate-only' }
Push-Location -LiteralPath $testRoot
try {
    & $testPython @testArguments
    $testExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $testExitCode
