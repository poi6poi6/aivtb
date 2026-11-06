param(
    [Parameter(Mandatory = $true)][int]$LauncherPid,
    [int]$TtsPid = 0,
    [string]$TtsIdentity = '',
    [int]$RemoveMapping = 0,
    [string]$Drive = "V:",
    [string]$RootToken = "",
    [string]$UiUrl = "",
    [int]$OpenBrowser = 1
)

$ErrorActionPreference = "SilentlyContinue"
$ProgressPreference = "SilentlyContinue"

function Test-HttpEndpoint([string]$Url) {
    if (-not $Url) { return $false }
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 1
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    }
    catch {
        return $false
    }
}

function Get-SubstTarget([string]$DriveName) {
    $deviceName = $DriveName.Trim().TrimEnd("\")
    if ($deviceName -notmatch "^[A-Za-z]:$") { return $null }

    if (-not ("AiVtuberSubstNative" -as [type])) {
        Add-Type -TypeDefinition @"
using System.Runtime.InteropServices;
using System.Text;

public static class AiVtuberSubstNative
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern uint QueryDosDevice(
        string lpDeviceName,
        StringBuilder lpTargetPath,
        int ucchMax
    );
}
"@
    }

    $buffer = New-Object Text.StringBuilder 32768
    $length = [AiVtuberSubstNative]::QueryDosDevice(
        $deviceName,
        $buffer,
        $buffer.Capacity
    )
    if ($length -eq 0) { return $null }

    $rawTarget = $buffer.ToString()
    $substPrefix = "\??\"
    if (-not $rawTarget.StartsWith($substPrefix, [StringComparison]::Ordinal)) {
        return $null
    }
    return $rawTarget.Substring($substPrefix.Length).TrimEnd("\")
}

$opened = $false
while (Get-Process -Id $LauncherPid -ErrorAction SilentlyContinue) {
    if (-not $opened -and $OpenBrowser -eq 1 -and (Test-HttpEndpoint $UiUrl)) {
        Start-Process $UiUrl
        $opened = $true
    }
    Start-Sleep -Milliseconds 500
}

if ($TtsPid -gt 0 -and $TtsIdentity) {
    $tts = Get-Process -Id $TtsPid -ErrorAction SilentlyContinue
    if ($tts -and ('win:' + [string]$tts.StartTime.ToFileTimeUtc()) -ceq $TtsIdentity) {
        Stop-Process -Id $TtsPid -ErrorAction SilentlyContinue
        Wait-Process -Id $TtsPid -Timeout 8 -ErrorAction SilentlyContinue
    }
}

if ($RemoveMapping -eq 1 -and $RootToken) {
    try {
        $expectedRoot = [Text.Encoding]::Unicode.GetString(
            [Convert]::FromBase64String($RootToken)
        ).TrimEnd("\")
        $actualRoot = Get-SubstTarget $Drive
        if ($actualRoot -and [string]::Equals(
            $actualRoot,
            $expectedRoot,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            & subst.exe $Drive /D | Out-Null
        }
    }
    catch {
        # The main launcher performs the same idempotent cleanup on normal exit.
    }
}
