param(
    [switch]$NoBrowser,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)).TrimEnd("\")
$drive = "V:"
$mainPython = Join-Path $root ".venv\Scripts\python.exe"
$indexPython = Join-Path $root "IndexTTS2\.venv\Scripts\python.exe"
$settingsPath = Join-Path $root "config\settings.yaml"
$fishSettingsTool = Join-Path $root "tools\configure_tts.py"
$cleanupScript = Join-Path $PSScriptRoot "launcher-cleanup.ps1"
$logDirectory = Join-Path $root "data\launcher"
$errorMarker = Join-Path $logDirectory "last-start-error.txt"
$ttsProcess = $null
$ttsIdentity = ''
$removeMappingOnExit = $false
$watchdog = $null
$exitCode = 0
$launchMutex = $null
$ownsLaunchMutex = $false

function Get-YamlScalar(
    [string]$Path,
    [string]$Section,
    [string]$Key,
    [string]$Default
) {
    $inside = $false
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^$([regex]::Escape($Section)):\s*(?:#.*)?$") {
            $inside = $true
            continue
        }
        if (-not $inside) { continue }
        if ($line -match "^[^\s#]") { break }
        if ($line -match "^\s+$([regex]::Escape($Key)):\s*([^#\s]+)") {
            return $Matches[1].Trim('"', "'")
        }
    }
    return $Default
}

function Test-HttpEndpoint([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -MaximumRedirection 0
        $health = $response.Content | ConvertFrom-Json
        return $response.StatusCode -eq 200 -and $health.status -ceq 'ok' -and $health.backend -ceq 'indextts2'
    }
    catch {
        return $false
    }
}

function Test-LoopbackPortUnused([string]$Url) {
    try {
        $uri = [Uri]$Url
        $address = $null
        $isLoopback = $uri.DnsSafeHost -eq "localhost"
        if ([Net.IPAddress]::TryParse($uri.DnsSafeHost, [ref]$address)) {
            $isLoopback = [Net.IPAddress]::IsLoopback($address)
        }
        if (-not $isLoopback) { return $false }
        # Inspect the OS listener table without opening a probe socket. Any
        # listener on this port is treated conservatively as occupied.
        $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        foreach ($listener in $listeners) {
            if ($listener.Port -eq $uri.Port) { return $false }
        }
        return $true
    }
    catch { return $false }
}

function Get-MainServiceState([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri "$($Url.TrimEnd('/'))/runtime-health" `
            -UseBasicParsing -TimeoutSec 2 -MaximumRedirection 0
    }
    catch {
        # Windows can report timeout even when loopback has no listener. Only
        # an OS-confirmed empty local port or explicit connection failure may
        # count as absent; a hung HTTP service must not trigger a duplicate.
        if ($_.Exception.Response) { return "occupied" }
        if ($_.Exception.Status -eq [Net.WebExceptionStatus]::ConnectFailure) { return "absent" }
        if (Test-LoopbackPortUnused $Url) { return "absent" }
        return "unavailable"
    }
    if ($response.StatusCode -ne 200) { return "occupied" }
    try {
        $health = $response.Content | ConvertFrom-Json
        if ($health.scope -cne "main" -or $health.boot_id -notmatch "^[a-fA-F0-9]{16}$") {
            return "occupied"
        }
        # Fingerprint the application, not the currently edited source hashes.
        # Readiness can be false while an optional task service is disconnected.
        foreach ($name in @("brain/main.py", "brain/webui.py", "brain/static/index.html")) {
            $hash = $health.code.PSObject.Properties[$name].Value
            if ($hash -notmatch "^[a-fA-F0-9]{64}$") { return "occupied" }
        }
        return "main"
    }
    catch {
        return "occupied"
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

function Remove-OwnedMapping {
    if (-not $removeMappingOnExit) { return }
    $target = Get-SubstTarget $drive
    if ($target -and [string]::Equals(
        $target,
        $root,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        Write-Host "[launcher] Removing temporary V: mapping..."
        & subst.exe $drive /D | Out-Null
    }
}

function Stop-OwnedTts {
    if (-not $ttsProcess) { return }
    $process = Get-Process -Id $ttsProcess.Id -ErrorAction SilentlyContinue
    # An exited child PID may have been reused while the text service stayed up.
    if ($process -and $ttsIdentity -and ('win:' + [string]$process.StartTime.ToFileTimeUtc()) -ceq $ttsIdentity) {
        Write-Host "[launcher] Stopping IndexTTS2..."
        Stop-Process -Id $ttsProcess.Id -ErrorAction SilentlyContinue
        Wait-Process -Id $ttsProcess.Id -Timeout 8 -ErrorAction SilentlyContinue
    }
}

try {
    foreach ($required in @($mainPython, $settingsPath, $cleanupScript)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Required file not found: $required"
        }
    }

    $ttsBackend = Get-YamlScalar $settingsPath "tts" "backend" "mock"
    $ttsUrl = Get-YamlScalar $settingsPath "tts" "indextts2_url" "http://127.0.0.1:9890"
    $webHost = Get-YamlScalar $settingsPath "webui" "host" "127.0.0.1"
    $webPort = Get-YamlScalar $settingsPath "webui" "port" "8080"
    if ($webHost -in @("0.0.0.0", "::")) { $webHost = "127.0.0.1" }
    $uiUrl = "http://${webHost}:${webPort}"

    if ($ValidateOnly) {
        if ($ttsBackend -eq "indextts2" -and -not (Test-Path -LiteralPath $indexPython)) {
            throw "IndexTTS2 Python not found: $indexPython"
        }
        if ($ttsBackend -eq "fish_speech" -and -not (Test-Path -LiteralPath $fishSettingsTool)) {
            throw "Fish Speech check component not found: $fishSettingsTool"
        }
        if ($ttsBackend -eq "indextts2") {
            $existing = Get-SubstTarget $drive
            $driveExists = Test-Path -LiteralPath "$drive\"
            if (-not $existing -and $driveExists) {
                throw "V: exists but is not a SUBST mapping. Choose another drive before launch."
            }
            if ($existing -and -not [string]::Equals(
                $existing,
                $root,
                [StringComparison]::OrdinalIgnoreCase
            )) {
                throw "V: is already mapped to another directory: $existing"
            }
        }
        Write-Host "[launcher] Validation OK. TTS=$ttsBackend UI=$uiUrl"
        exit 0
    }

    # Hold one project-specific lock for the entire main-process lifetime,
    # including model loading before the HTTP endpoint becomes available.
    $rootBytes = [Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($root).ToUpperInvariant())
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $rootHash = [BitConverter]::ToString($hasher.ComputeHash($rootBytes)).Replace("-", "") }
    finally { $hasher.Dispose() }
    $launchMutex = New-Object Threading.Mutex($false, "Local\AiVtuberMain_$rootHash")
    try { $ownsLaunchMutex = $launchMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsLaunchMutex = $true }

    $mainState = Get-MainServiceState $uiUrl
    if ($mainState -eq "main") {
        Write-Host "[launcher] Reusing the existing AI VTuber service: $uiUrl"
        if (-not $NoBrowser) { Start-Process -FilePath $uiUrl | Out-Null }
        exit 0
    }
    if (-not $ownsLaunchMutex) {
        Write-Host "[launcher] This project's launcher is already starting or running. No duplicate will be started."
        exit 0
    }
    if ($mainState -eq "occupied") {
        throw "The configured UI address is occupied but does not identify an AI VTuber main service: $uiUrl"
    }
    if ($mainState -eq "unavailable") {
        throw "Unable to check the configured UI address safely. No duplicate will be started: $uiUrl"
    }
    if (Test-Path -LiteralPath $errorMarker) {
        Remove-Item -LiteralPath $errorMarker -Force -ErrorAction SilentlyContinue
    }

    # TTS is an optional capability. Its health check must never delay or
    # prevent the text UI / QQ core from starting. The main process probes it.
    $ttsAlreadyRunning = $false
    $env:AI_VTUBER_TTS_BACKEND = $ttsBackend
    $env:AI_VTUBER_TTS_STARTUP_STATE = 'external'
    $env:AI_VTUBER_TTS_OWNED_PID = ''
    $env:AI_VTUBER_TTS_OWNED_IDENTITY = ''
    try {
        if ($ttsBackend -eq "indextts2") {
            if (-not (Test-Path -LiteralPath $indexPython)) {
                throw "IndexTTS2 Python not found: $indexPython"
            }
            $ttsAlreadyRunning = Test-HttpEndpoint "$($ttsUrl.TrimEnd('/'))/health"
            $existing = Get-SubstTarget $drive
            if (-not $existing) {
                $driveExists = Test-Path -LiteralPath "$drive\"
                if ($driveExists) {
                    throw "V: exists but is not a SUBST mapping. It will not be changed."
                }
                Write-Host "[launcher] Creating temporary V: mapping..."
                & subst.exe $drive $root
                if ($LASTEXITCODE -ne 0) {
                    throw "Unable to create V: mapping."
                }
                $removeMappingOnExit = -not $ttsAlreadyRunning
            }
            elseif (-not [string]::Equals(
                $existing,
                $root,
                [StringComparison]::OrdinalIgnoreCase
            )) {
                throw "V: is already mapped to another directory: $existing"
            }
            elseif (-not $ttsAlreadyRunning) {
                # Adopt a stale mapping left by an older launcher and clean it later.
                $removeMappingOnExit = $true
            }

            if (-not (Test-Path -LiteralPath "V:\IndexTTS2\.venv\Scripts\python.exe")) {
                throw "V: does not point to this project."
            }

            if (-not $ttsAlreadyRunning) {
                New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
                $ttsUri = [Uri]$ttsUrl
                if ($ttsUri.Host -notin @("127.0.0.1", "localhost")) {
                    throw "Automatic IndexTTS2 launch only supports a local URL: $ttsUrl"
                }
                $env:HF_ENDPOINT = "https://hf-mirror.com"
                Write-Host "[launcher] Starting IndexTTS2. First model load may take a while..."
                $ttsProcess = Start-Process `
                    -FilePath "V:\IndexTTS2\.venv\Scripts\python.exe" `
                    -ArgumentList @(
                        "-u", "V:\indextts2_server.py",
                        "--project-dir", "V:\IndexTTS2",
                        "--host", "127.0.0.1",
                        "--port", [string]$ttsUri.Port,
                        "--fp16"
                    ) `
                    -WorkingDirectory "V:\" `
                    -WindowStyle Hidden `
                    -RedirectStandardOutput (Join-Path $logDirectory "indextts2.stdout.log") `
                    -RedirectStandardError (Join-Path $logDirectory "indextts2.stderr.log") `
                    -PassThru

                $env:AI_VTUBER_TTS_STARTUP_STATE = 'loading'
                $env:AI_VTUBER_TTS_OWNED_PID = [string]$ttsProcess.Id
                try { $ttsIdentity = 'win:' + [string]$ttsProcess.StartTime.ToFileTimeUtc() }
                catch { $ttsIdentity = '' }
                $env:AI_VTUBER_TTS_OWNED_IDENTITY = $ttsIdentity
                Write-Host '[launcher] IndexTTS2 is loading in the background; text services start now.'
            }
            else {
                $env:AI_VTUBER_TTS_STARTUP_STATE = 'ready'
                Write-Host "[launcher] Reusing the existing IndexTTS2 service."
            }
        }
        elseif ($ttsBackend -eq "fish_speech") {
            Write-Host "[launcher] Reusing external Fish Speech; health is checked independently and this launcher does not stop it."
        }
        else {
            Write-Host "[launcher] TTS backend is '$ttsBackend'; local IndexTTS2 will not be started."
        }

    }
    catch {
        $env:AI_VTUBER_TTS_STARTUP_STATE = 'unavailable'
        Write-Host '[launcher] TTS could not be started. Text services will remain available.'
        New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction SilentlyContinue | Out-Null
        Set-Content -LiteralPath (Join-Path $logDirectory 'last-tts-start-error.txt') `
            -Value $_.Exception.Message -Encoding UTF8 -ErrorAction SilentlyContinue
    }

    $rootToken = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($root))
    $ttsPid = if ($ttsProcess) { $ttsProcess.Id } else { 0 }
    $watchdogArguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $cleanupScript + '"'),
        "-LauncherPid", [string]$PID,
        "-TtsPid", [string]$ttsPid,
        "-RemoveMapping", $(if ($removeMappingOnExit) { "1" } else { "0" }),
        "-Drive", $drive,
        "-RootToken", $rootToken,
        "-UiUrl", $uiUrl,
        "-OpenBrowser", $(if ($NoBrowser) { "0" } else { "1" })
    )
    if ($ttsIdentity) { $watchdogArguments += @('-TtsIdentity', $ttsIdentity) }
    $watchdogHost = Join-Path $PSHOME "powershell.exe"
    if (-not (Test-Path -LiteralPath $watchdogHost)) {
        $watchdogHost = Join-Path $PSHOME "pwsh.exe"
    }
    if (-not (Test-Path -LiteralPath $watchdogHost)) {
        throw "Unable to find a PowerShell executable for the cleanup watchdog."
    }
    $watchdog = Start-Process `
        -FilePath $watchdogHost `
        -ArgumentList $watchdogArguments `
        -WindowStyle Hidden `
        -PassThru

    Write-Host "[launcher] Starting AI VTuber. Press Ctrl+C to stop."
    # The external release controller waits for this exact launcher and its
    # cleanup helper before replacing code or starting another generation.
    $env:AI_VTUBER_LAUNCHER_PID = [string]$PID
    $env:AI_VTUBER_LAUNCHER_IDENTITY = 'win:' + [string](Get-Process -Id $PID).StartTime.ToFileTimeUtc()
    $env:AI_VTUBER_CLEANUP_PID = [string]$watchdog.Id
    $env:AI_VTUBER_CLEANUP_IDENTITY = ''
    try { $env:AI_VTUBER_CLEANUP_IDENTITY = 'win:' + [string]$watchdog.StartTime.ToFileTimeUtc() }
    catch { Write-Host '[launcher] Cleanup identity unavailable; automatic release activation is disabled.' }
    Push-Location -LiteralPath $root
    try {
        & $mainPython -m brain.main
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
catch {
    $errorMessage = $_.Exception.Message
    Write-Host "[launcher] ERROR: $errorMessage" -ForegroundColor Red
    if (-not $ValidateOnly) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        Set-Content -LiteralPath $errorMarker -Value $errorMessage -Encoding UTF8
    }
    $exitCode = 1
}
finally {
    Stop-OwnedTts
    Remove-OwnedMapping
    if ($launchMutex) {
        if ($ownsLaunchMutex) { $launchMutex.ReleaseMutex() }
        $launchMutex.Dispose()
    }
}

exit $exitCode
