param([switch]$CheckService)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$settingsTool = Join-Path $projectRoot 'tools\configure_tts.py'
try {
    foreach ($required in @($python, $settingsTool)) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Required file not found: $required" }
    }
    $options = @()
    if ($CheckService) { $options += '--check' }
    & $python $settingsTool @options
    exit $LASTEXITCODE
}
catch {
    Write-Host ('[launcher] TTS settings failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
