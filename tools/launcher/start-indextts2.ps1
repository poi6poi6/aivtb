$ErrorActionPreference = "Stop"
$root = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)).TrimEnd("\")
$env:HF_ENDPOINT = "https://hf-mirror.com"
# Windows PowerShell 5.1 treats UTF-8 files without a BOM as an ANSI code page.
# Keep this launcher ASCII-only so a workspace path containing CJK remains safe.
$mappedPython = "V:\IndexTTS2\.venv\Scripts\python.exe"
$createdMapping = $false
$ttsExitCode = 1

function Get-SubstTarget([string]$DriveName) {
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
    $length = [AiVtuberSubstNative]::QueryDosDevice($DriveName, $buffer, $buffer.Capacity)
    if ($length -eq 0) { return $null }
    $rawTarget = $buffer.ToString()
    $substPrefix = "\??\"
    if (-not $rawTarget.StartsWith($substPrefix, [StringComparison]::Ordinal)) {
        return $null
    }
    return $rawTarget.Substring($substPrefix.Length).TrimEnd("\")
}

try {
    $existing = Get-SubstTarget "V:"
    if ($existing) {
        if (-not [string]::Equals($existing, $root, [StringComparison]::OrdinalIgnoreCase)) {
            throw "V: is already mapped to another directory: $existing"
        }
    }
    elseif (Test-Path -LiteralPath "V:\") {
        throw "V: exists but is not a SUBST mapping. It will not be changed."
    }
    else {
        & subst.exe V: $root
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to create the V: path mapping. Make sure drive V: is available."
        }
        $createdMapping = $true
    }
    $verifiedRoot = Get-SubstTarget "V:"
    if (-not [string]::Equals($verifiedRoot, $root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "V: does not point to this project. It will not be used."
    }
    if (-not (Test-Path -LiteralPath $mappedPython)) {
        throw "IndexTTS2 Python not found: $mappedPython"
    }
    & $mappedPython -u `
        V:\indextts2_server.py `
        --project-dir V:\IndexTTS2 `
        --host 127.0.0.1 `
        --port 9890 `
        --fp16
    $ttsExitCode = $LASTEXITCODE
}
finally {
    if ($createdMapping) {
        $actualRoot = Get-SubstTarget "V:"
        if ($actualRoot -and [string]::Equals(
            $actualRoot,
            $root,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            & subst.exe V: /D | Out-Null
        }
    }
}
exit $ttsExitCode
