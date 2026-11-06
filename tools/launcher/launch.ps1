param(
    [ValidateSet('menu', 'run', 'local-test', 'check', 'status', 'logs',
        'tasks-start', 'tasks-stop', 'minecraft-start', 'minecraft-stop', 'tts', 'friend-stop',
        'voice-settings', 'fish-check')]
    [string]$Action = 'menu',
    [ValidateRange(1024, 65535)][int]$Port = 18080,
    [switch]$NoBrowser,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$scriptHost = Join-Path $PSHOME 'powershell.exe'
if (-not (Test-Path -LiteralPath $scriptHost)) {
    $scriptHost = Join-Path $PSHOME 'pwsh.exe'
}

function Invoke-LauncherScript([string]$Name, [string[]]$Options = @()) {
    $scriptPath = Join-Path $PSScriptRoot $Name
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "启动组件缺失：$scriptPath"
    }
    # A child host preserves existing exit and watchdog ownership semantics.
    & $scriptHost -NoProfile -ExecutionPolicy Bypass -File $scriptPath @Options | Out-Host
    return [int]$LASTEXITCODE
}

function Get-MainPort {
    $settings = Join-Path $projectRoot 'config\settings.yaml'
    if (-not (Test-Path -LiteralPath $settings)) { return 8080 }
    $insideWeb = $false
    foreach ($line in Get-Content -LiteralPath $settings -Encoding UTF8) {
        if ($line -match '^webui:\s*(?:#.*)?$') { $insideWeb = $true; continue }
        if (-not $insideWeb) { continue }
        if ($line -match '^[^\s#]') { break }
        if ($line -match '^\s+port:\s*["'']?(\d+)["'']?\s*(?:#.*)?$') {
            $configuredPort = [int]$Matches[1]
            if ($configuredPort -gt 0 -and $configuredPort -le 65535) { return $configuredPort }
        }
    }
    return 8080
}

function Show-LauncherStatus {
    Write-Host "`n项目：$projectRoot"
    $pythonPresent = Test-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe')
    Write-Host ('Python 环境：' + $(if ($pythonPresent) { '已找到' } else { '缺失，请先完成安装' }))
    $mainPort = Get-MainPort
    try {
        $listeners = @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners())
        foreach ($item in @(
            @{ Label = '正式控制台'; Port = $mainPort; Url = "http://127.0.0.1:$mainPort/?console=1" },
            @{ Label = '离线测试台'; Port = $Port; Url = "http://127.0.0.1:$Port/?console=1" },
            @{ Label = '后台任务台'; Port = 8085; Url = 'http://127.0.0.1:8085/tasks' },
            @{ Label = 'Minecraft'; Port = 25565; Url = '127.0.0.1:25565' }
        )) {
            $listening = @($listeners | Where-Object { $_.Port -eq $item.Port }).Count -gt 0
            $state = if ($listening) { '端口有监听' } else { '端口无监听' }
            Write-Host ('  {0}：{1}  {2}' -f $item.Label, $state, $item.Url)
        }
        Write-Host '端口监听仅供排查，不代表是本项目服务或已经就绪。'
    }
    catch {
        Write-Host '暂时无法读取端口状态，可到控制台的运行状态页检查。' -ForegroundColor Yellow
    }
    Write-Host ('日志位置：' + (Join-Path $projectRoot 'data\launcher'))
}

function Invoke-LauncherAction([string]$SelectedAction) {
    switch ($SelectedAction) {
        'run' {
            $options = @()
            if ($NoBrowser) { $options += '-NoBrowser' }
            if ($ValidateOnly) { $options += '-ValidateOnly' }
            return Invoke-LauncherScript 'start-all.ps1' $options
        }
        'local-test' {
            $options = @('-Port', [string]$Port)
            if ($NoBrowser) { $options += '-NoBrowser' }
            if ($ValidateOnly) { $options += '-ValidateOnly' }
            return Invoke-LauncherScript 'start-local-test.ps1' $options
        }
        'check' {
            Write-Host '检查正式运行条件（不启动服务）...'
            $mainCode = Invoke-LauncherScript 'start-all.ps1' @('-ValidateOnly')
            Write-Host '检查离线测试条件（不启动服务）...'
            $testCode = Invoke-LauncherScript 'start-local-test.ps1' @('-ValidateOnly', '-Port', [string]$Port)
            if ($mainCode -ne 0 -or $testCode -ne 0) { return 1 }
            Write-Host '启动条件检查通过。' -ForegroundColor Green
            return 0
        }
        'status' { Show-LauncherStatus; return 0 }
        'logs' {
            $logFolder = Join-Path $projectRoot 'data\launcher'
            New-Item -ItemType Directory -Path $logFolder -Force | Out-Null
            # Explorer is the explicit user-selected interactive output.
            Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $logFolder + '"')
            Write-Host "已打开日志目录：$logFolder"
            return 0
        }
        'tasks-start' { return Invoke-LauncherScript 'start-agent-tasks.ps1' }
        'tasks-stop' { return Invoke-LauncherScript 'stop-agent-tasks.ps1' }
        'minecraft-start' { return Invoke-LauncherScript 'start-minecraft.ps1' }
        'minecraft-stop' { return Invoke-LauncherScript 'stop-minecraft.ps1' }
        'tts' { return Invoke-LauncherScript 'start-indextts2.ps1' }
        'voice-settings' { return Invoke-LauncherScript 'tts-settings.ps1' }
        'fish-check' { return Invoke-LauncherScript 'tts-settings.ps1' @('-CheckService') }
        'friend-stop' { return Invoke-LauncherScript 'stop-friend-test.ps1' }
        default { throw "未知启动操作：$SelectedAction" }
    }
}

function Read-AdvancedAction {
    Write-Host "`n高级操作"
    Write-Host '  1  启动后台任务台       2  停止后台任务台并取消任务'
    Write-Host '  3  启动 Minecraft       4  保存并停止 Minecraft'
    Write-Host '  5  单独启动 IndexTTS2   6  停止朋友测试'
    Write-Host '  7  检查 Fish Speech 服务（不合成、不切换模型）'
    Write-Host '  0  返回'
    $advancedSelection = Read-Host '选择操作'
    if ($null -eq $advancedSelection) { return '' }
    switch ($advancedSelection.Trim()) {
        '1' { return 'tasks-start' }
        '2' { return 'tasks-stop' }
        '3' { return 'minecraft-start' }
        '4' { return 'minecraft-stop' }
        '5' { return 'tts' }
        '6' { return 'friend-stop' }
        '7' { return 'fish-check' }
        default { return '' }
    }
}

try {
    if ($ValidateOnly -and $Action -notin @('menu', 'run', 'local-test', 'check')) {
        throw '-ValidateOnly 仅用于 run、local-test 或 check，不会执行其他操作。'
    }
    if ($Action -eq 'menu' -and $ValidateOnly) { $Action = 'check' }
    if ($Action -ne 'menu') { exit (Invoke-LauncherAction $Action) }
    while ($true) {
        Write-Host "`n========== 助手 · 启动中心 ==========" -ForegroundColor Cyan
        Write-Host '  1  正式运行（主服务 + 已配置的语音）'
        Write-Host '  2  本地离线测试（脚本回复，无需模型）'
        Write-Host '  3  检查启动环境'
        Write-Host '  4  查看本机状态'
        Write-Host '  5  打开启动日志'
        Write-Host '  6  高级操作（后台任务 / Minecraft / 单独语音）'
        Write-Host '  7  语音模型设置（Fish Speech / IndexTTS2 / 其他）'
        Write-Host '  0  退出'
        Write-Host '运行服务时请保留当前窗口；停止服务后可重新打开启动中心。'
        $selection = Read-Host '请选择，直接回车退出'
        if ($null -eq $selection) { break }
        $selection = $selection.Trim()
        if (-not $selection -or $selection -eq '0') { break }
        $selectedAction = switch ($selection) {
            '1' { 'run' }
            '2' { 'local-test' }
            '3' { 'check' }
            '4' { 'status' }
            '5' { 'logs' }
            '6' { Read-AdvancedAction }
            '7' { 'voice-settings' }
            default { Write-Host '请输入菜单中的数字。' -ForegroundColor Yellow; '' }
        }
        if (-not $selectedAction) { continue }
        try {
            $resultCode = Invoke-LauncherAction $selectedAction
            if ($resultCode -ne 0) {
                Write-Host "操作未完成（退出码 $resultCode），请查看上方提示。" -ForegroundColor Red
            }
        }
        catch { Write-Host $_.Exception.Message -ForegroundColor Red }
    }
    exit 0
}
catch {
    Write-Host ('启动失败：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
