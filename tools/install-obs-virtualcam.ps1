$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$obsRoot = Join-Path $projectRoot 'data\obs-studio'
$resultPath = Join-Path $obsRoot 'virtualcam-install-result.json'
$result = @{ success = $false; timestamp = (Get-Date).ToString('o') }
try {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Administrator rights are required to register the OBS virtual camera.'
    }
    foreach ($bits in @(32, 64)) {
        $modulePath = Join-Path $obsRoot "core\win-dshow\data\obs-virtualcam-module$bits.dll"
        if ((Get-AuthenticodeSignature -FilePath $modulePath).Status -ne 'Valid') {
            throw "Invalid OBS virtual camera signature: $bits-bit"
        }
        $systemDir = if ($bits -eq 32) { 'SysWOW64' } else { 'System32' }
        $registrar = Join-Path $env:WINDIR "$systemDir\regsvr32.exe"
        $registration = Start-Process -FilePath $registrar -ArgumentList @('/i', '/s', ('"' + $modulePath + '"')) -WindowStyle Hidden -Wait -PassThru
        if ($registration.ExitCode -ne 0) {
            throw "OBS virtual camera registration failed: $bits-bit, exit $($registration.ExitCode)"
        }
        $classRoot = if ($bits -eq 32) { 'SOFTWARE\Classes\WOW6432Node\CLSID' } else { 'SOFTWARE\Classes\CLSID' }
        $keyPath = "Registry::HKEY_LOCAL_MACHINE\$classRoot\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}\InprocServer32"
        $registeredPath = (Get-Item -LiteralPath $keyPath).GetValue('')
        if ([IO.Path]::GetFullPath($registeredPath) -ne [IO.Path]::GetFullPath($modulePath)) {
            throw "OBS virtual camera registration path does not match: $bits-bit"
        }
    }
    $result.success = $true
}
catch {
    $result.error = $_.Exception.Message
}
$result | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
if (-not $result.success) { exit 1 }
