param(
    [string]$ProjectRoot = "",
    [string]$UvPath = "",
    [string]$CollectorMode = "",
    [switch]$Doctor,
    [switch]$PrepareOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
else {
    $ProjectRoot = (Resolve-Path $ProjectRoot).Path
}
. (Join-Path $PSScriptRoot "launcher_common.ps1")

$script:RuntimeRoot = Join-Path $ProjectRoot ".runtime\chat_collector"
$script:CollectorEntry = Join-Path $ProjectRoot "scripts\run_chat_collector.py"
$script:ActiveCollectorMode = "4"
$script:CollectorSlots = @("A", "B", "C", "D")
$script:AccountsPath = Join-Path $script:RuntimeRoot "accounts.json"
$script:RegionalSlots = @{
    "A" = "ZH"; "B" = "FR"; "C" = "DE"; "D" = "ES"
    "E" = "PT"; "F" = "RU"; "G" = "JA"; "H" = "KO"
    "I" = "TC"; "J" = "IT"; "K" = "PL"; "L" = "UK"
    "M" = "EN_NA"; "N" = "EN_EU"; "O" = "EN_SA"
    "P" = "EN_RU"; "Q" = "EN_AS"
}
$script:ActiveStatuses = @(
    "authenticating", "joining", "listening", "degraded", "probing", "reconnecting"
)
$script:StatusLabels = @{
    "not_started" = "等待取票"
    "needs_ticket" = "等待取票"
    "authenticating" = "正在认证"
    "reconnecting" = "正在自动重连"
    "probing" = "正在执行协议探测"
    "joining" = "正在加入频道"
    "listening" = "正常采集中"
    "degraded" = "部分频道可用（正在重试）"
    "stopped" = "已停止"
    "stop_requested" = "正在停止"
    "auth_failed" = "认证失败，需要重新取票"
    "ticket_error" = "票据异常，需要重新取票"
    "config_error" = "配置错误"
    "storage_error" = "文件读写错误"
    "internal_error" = "内部错误"
    "disconnected" = "连接已断开，需要重新取票"
    "failed" = "频道加入失败，需要重新取票"
}

function Set-CollectorContext {
    param([string]$Mode)

    if ($Mode -notin @("4", "17")) {
        throw "采集模式必须是 4 或 17。"
    }
    $script:ActiveCollectorMode = $Mode
    if ($Mode -eq "17") {
        $script:CollectorSlots = @(
            "A", "B", "C", "D", "E", "F", "G", "H", "I",
            "J", "K", "L", "M", "N", "O", "P", "Q"
        )
        $script:AccountsPath = Join-Path $script:RuntimeRoot "accounts_17.json"
    }
    else {
        $script:CollectorSlots = @("A", "B", "C", "D")
        $script:AccountsPath = Join-Path $script:RuntimeRoot "accounts.json"
    }
}

function Read-CollectorMode {
    $path = Join-Path $script:RuntimeRoot "collector_mode.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return "4"
    }
    try {
        $document = Get-Content -LiteralPath $path -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
        $mode = ([string]$document.mode).Trim()
        if ($mode -in @("4", "17")) {
            return $mode
        }
    }
    catch {
    }
    throw "采集模式文件无法读取：$path"
}

function Set-CollectorModeValue {
    param([string]$UvPath, [string]$Mode)

    Invoke-UvChecked `
        -UvPath $UvPath `
        -ProjectRoot $ProjectRoot `
        -Arguments @(
            "run", "--no-sync", "python", "scripts/run_chat_collector.py",
            "mode", $Mode, "--runtime-root", $script:RuntimeRoot
        ) `
        -FailureMessage "采集模式切换失败"
    Set-CollectorContext -Mode $Mode
    Write-Success "已切换为 $Mode 槽采集模式"
}

function Initialize-CollectorFiles {
    param([string]$UvPath)

    Write-Step "初始化采集运行目录"
    Invoke-UvChecked `
        -UvPath $UvPath `
        -ProjectRoot $ProjectRoot `
        -Arguments @(
            "run", "--no-sync", "python", "scripts/run_chat_collector.py",
            "init", "--runtime-root", $script:RuntimeRoot
        ) `
        -FailureMessage "采集目录初始化失败"
}

function Read-CollectorAccounts {
    $path = $script:AccountsPath
    try {
        return Get-Content -LiteralPath $path -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "账号配置无法读取：$path。可删除该文件后重新运行启动器。"
    }
}

function Test-CollectorAccountsNeedWizard {
    param($Accounts)

    $used = New-Object 'System.Collections.Generic.HashSet[string]' `
        ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($slot in $script:CollectorSlots) {
        $entry = $Accounts.PSObject.Properties[$slot]
        if ($null -eq $entry) {
            return $true
        }
        if ($null -eq $entry.Value) {
            return $true
        }
        $nickProperty = $entry.Value.PSObject.Properties["nick"]
        if ($null -eq $nickProperty) {
            return $true
        }
        $nick = ([string]$nickProperty.Value).Trim()
        if ([string]::IsNullOrWhiteSpace($nick) -or
            $nick -match '^account-[a-q]$' -or
            $nick -match '\s' -or
            -not $used.Add($nick)) {
            return $true
        }
    }
    return $false
}

function Ensure-CollectorAccounts {
    $path = $script:AccountsPath
    $accounts = Read-CollectorAccounts
    if (-not (Test-CollectorAccountsNeedWizard $accounts)) {
        return $accounts
    }

    Write-Host ""
    if ($script:ActiveCollectorMode -eq "4") {
        Write-Host "首次运行需要填写四个游戏账号的昵称。" -ForegroundColor Yellow
        Write-Host "昵称必须与登录游戏时完全一致，四个槽请使用不同账号。" -ForegroundColor Gray
    }
    else {
        Write-Host "17 槽模式需要填写十七个游戏账号的昵称。" -ForegroundColor Yellow
        Write-Host "每个账号固定负责一个地区的 G/Q/R/T 四频道，账号不能重复。" -ForegroundColor Gray
    }
    $document = [ordered]@{}
    $used = New-Object 'System.Collections.Generic.HashSet[string]' `
        ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($slot in $script:CollectorSlots) {
        while ($true) {
            $label = if ($script:ActiveCollectorMode -eq "17") {
                "槽 $slot / $($script:RegionalSlots[$slot]) 的游戏昵称"
            }
            else {
                "槽 $slot 的游戏昵称"
            }
            $nick = (Read-Host $label).Trim()
            if ([string]::IsNullOrWhiteSpace($nick) -or $nick -match '\s') {
                Write-Host "昵称不能为空，也不能含空格。" -ForegroundColor Red
                continue
            }
            if (-not $used.Add($nick)) {
                Write-Host "该昵称已经填写过；每个槽必须是不同账号。" -ForegroundColor Red
                continue
            }
            $document[$slot] = [ordered]@{ nick = $nick }
            break
        }
    }
    $json = $document | ConvertTo-Json -Depth 3
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $json + [Environment]::NewLine, $encoding)
    if ($script:ActiveCollectorMode -eq "4") {
        Write-Success "四槽账号已保存到运行目录"
    }
    else {
        Write-Success "十七槽账号已保存到运行目录"
    }
    return Read-CollectorAccounts
}

function Test-PskFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    return (Get-Item -LiteralPath $Path).Length -ge 64
}

function Ensure-CollectorPsk {
    $destination = Join-Path $script:RuntimeRoot "psk_current.bin"
    if (Test-PskFile $destination) {
        return
    }

    $rootCandidate = Join-Path $ProjectRoot "psk_current.bin"
    if (Test-PskFile $rootCandidate) {
        Copy-Item -LiteralPath $rootCandidate -Destination $destination -Force
        Write-Success "已把项目根目录中的 psk_current.bin 放入采集运行目录"
        return
    }

    Write-Host ""
    Write-Host "还缺少已验证的 psk_current.bin。" -ForegroundColor Yellow
    Write-Host "请把该文件复制到下方已打开的文件夹：" -ForegroundColor White
    Write-Host "  $script:RuntimeRoot" -ForegroundColor White
    Start-Process explorer.exe -ArgumentList @("`"$script:RuntimeRoot`"")
    while (-not (Test-PskFile $destination)) {
        $answer = Read-Host "复制完成后按回车检查；输入 Q 可取消"
        if ($answer.Trim().ToUpperInvariant() -eq "Q") {
            throw "已取消；没有有效 PSK 时不能启动聊天采集。"
        }
        if (-not (Test-PskFile $destination)) {
            Write-Host "仍未找到有效文件（至少 64 字节），请确认文件名完全正确。" -ForegroundColor Red
        }
    }
    Write-Success "PSK 文件已就绪"
}

function Enable-BotCollectorFeed {
    $envPath = Join-Path $ProjectRoot ".env"
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        Copy-Item `
            -LiteralPath (Join-Path $ProjectRoot ".env.example") `
            -Destination $envPath
    }
    $feedPath = (Join-Path $script:RuntimeRoot "feed").Replace('\', '/')
    $cursorPath = (Join-Path $script:RuntimeRoot "feed_cursor.json").Replace('\', '/')
    Write-DotEnvValue -Path $envPath -Key "IRC_FEED_ENABLED" -Value "true"
    Write-DotEnvValue -Path $envPath -Key "IRC_FEED_DIR" -Value $feedPath
    Write-DotEnvValue -Path $envPath -Key "IRC_FEED_CHECKPOINT_PATH" -Value $cursorPath
    Write-DotEnvDefault -Path $envPath -Key "SEND_QUEUE_MAXSIZE" -Value "1000"
    Write-DotEnvDefault -Path $envPath -Key "TRADE_MESSAGE_TTL_SECONDS" -Value "60"
    Write-DotEnvDefault -Path $envPath -Key "SEND_MAX_RETRIES" -Value "1"
    Write-DotEnvDefault -Path $envPath -Key "SEND_RETRY_DELAY_SECONDS" -Value "2"
    Write-Success "已启用全部 68 个频道的严格紫卡链接输入（若 BOT 正在运行，请稍后重启 BOT）"
}

function Validate-Collector {
    param([string]$UvPath)
    if ($script:ActiveCollectorMode -eq "4") {
        Write-Step "验证四槽账号、频道分片和 PSK"
    }
    else {
        Write-Step "验证十七槽账号、单地区分片、自动快照关闭状态和 PSK"
    }
    Invoke-UvChecked `
        -UvPath $UvPath `
        -ProjectRoot $ProjectRoot `
        -Arguments @(
            "run", "--no-sync", "python", "scripts/run_chat_collector.py",
            "validate", "--runtime-root", $script:RuntimeRoot
        ) `
        -FailureMessage "采集配置验证失败"
    Write-Success "采集配置验证通过"
}

function Get-CollectorStatus {
    param([string]$UvPath)

    Push-Location $ProjectRoot
    try {
        $output = & $UvPath run --no-sync python scripts/run_chat_collector.py `
            status --runtime-root $script:RuntimeRoot 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw "无法读取采集状态（退出码 $exitCode）。"
    }
    $text = (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
    $jsonStart = $text.IndexOf("{")
    $jsonEnd = $text.LastIndexOf("}")
    try {
        if ($jsonStart -lt 0 -or $jsonEnd -lt $jsonStart) {
            throw "没有找到 JSON 对象"
        }
        return $text.Substring($jsonStart, $jsonEnd - $jsonStart + 1) |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "采集状态输出无法解析。原始输出：$text"
    }
}

function Show-CollectorStatus {
    param($Status)

    Write-Host ""
    if ($Status.supervisor_running) {
        Write-Host "监督器：运行中（PID $($Status.supervisor_pid)）" -ForegroundColor Green
    }
    else {
        Write-Host "监督器：未运行" -ForegroundColor Yellow
    }
    if ($script:ActiveCollectorMode -eq "17") {
        Write-Host "采集模式：17 槽" -ForegroundColor DarkGray
    }
    $control = Get-ObjectPropertyValue $Status "control" $null
    $controlRunId = [string](Get-ObjectPropertyValue $control "run_id" "")
    if (-not [string]::IsNullOrWhiteSpace($controlRunId)) {
        $gitCommit = "unknown"
        $gitValue = [string](Get-ObjectPropertyValue $control "git_commit" "")
        if (-not [string]::IsNullOrWhiteSpace($gitValue)) {
            $gitCommit = $gitValue
        }
        Write-Host (
            "采集代次：{0} / {1} / Git {2}" -f `
            $controlRunId,
            (Get-ObjectPropertyValue $control "status" "unknown"),
            $gitCommit
        ) -ForegroundColor DarkGray
    }
    $delivery = Get-ObjectPropertyValue $Status "delivery" $null
    $deliveryRunId = [string](Get-ObjectPropertyValue $delivery "run_id" "")
    if (-not [string]::IsNullOrWhiteSpace($deliveryRunId)) {
        Write-Host (
            "IRC 投递：接收={0} / 排队={1} / 在途={2}" -f `
            (Get-ObjectPropertyValue $delivery "accepting" $false),
            (Get-ObjectPropertyValue $delivery "irc_queued" 0),
            (Get-ObjectPropertyValue $delivery "irc_inflight" 0)
        ) -ForegroundColor DarkGray
    }
    Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
    $displaySlots = @($script:CollectorSlots)
    $expectedProperty = $Status.PSObject.Properties["expected_slots"]
    if ($null -ne $expectedProperty -and @($expectedProperty.Value).Count -gt 0) {
        $displaySlots = @($expectedProperty.Value)
    }
    foreach ($slot in $displaySlots) {
        $state = $Status.slots.PSObject.Properties[$slot].Value
        $statusKey = [string]$state.status
        $statusLabel = $script:StatusLabels[$statusKey]
        if ([string]::IsNullOrWhiteSpace($statusLabel)) {
            $statusLabel = $statusKey
        }
        $live = if ($state.process_alive) { "进程运行中" } else { "未运行" }
        $color = if ($state.process_alive -and $statusKey -eq "listening") {
            "Green"
        }
        elseif ($state.process_alive -and $statusKey -in @(
            "authenticating", "joining", "probing", "reconnecting"
        )) {
            "Cyan"
        }
        elseif ($statusKey -in @(
            "auth_failed", "ticket_error", "config_error", "storage_error",
            "internal_error", "disconnected", "failed"
        )) {
            "Red"
        }
        else {
            "Yellow"
        }
        $slotLabel = if ($script:ActiveCollectorMode -eq "17") {
            "$slot / $($script:RegionalSlots[$slot])"
        }
        else {
            $slot
        }
        Write-Host ("槽 {0}：{1} / {2}" -f $slotLabel, $statusLabel, $live) -ForegroundColor $color
        $detail = ""
        foreach ($propertyName in @("error", "reason", "outcome")) {
            $property = $state.PSObject.Properties[$propertyName]
            if ($null -ne $property -and
                -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
                $detail = [string]$property.Value
                break
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($detail)) {
            Write-Host "       原因：$detail" -ForegroundColor DarkGray
        }
        if ($null -ne $state.PSObject.Properties["privmsg_count"]) {
            $saved = [int]$state.privmsg_count
            $filtered = 0
            if ($null -ne $state.PSObject.Properties["filtered_privmsg_count"]) {
                $filtered = [int]$state.filtered_privmsg_count
            }
            $joined = 0
            if ($null -ne $state.PSObject.Properties["joined"]) {
                $joined = @($state.joined).Count
            }
            Write-Host (
                "       频道：{0} 个；已保存紫卡链接：{1} 条；已过滤普通消息：{2} 条" -f `
                $joined, $saved, $filtered
            ) -ForegroundColor DarkGray
        }
    }
    Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
}

function Start-CollectorSupervisor {
    param([string]$UvPath)

    $status = Get-CollectorStatus -UvPath $UvPath
    if ($status.supervisor_running) {
        $control = Get-ObjectPropertyValue $status "control" $null
        $sourceValue = [string](Get-ObjectPropertyValue `
            $control "source_mtime_utc" "")
        if (-not [string]::IsNullOrWhiteSpace($sourceValue)) {
            $loadedSource = [datetime]::Parse(
                $sourceValue).ToUniversalTime()
            $currentSource = Get-ProjectSourceTimestamp -ProjectRoot $ProjectRoot
            if ($currentSource -gt $loadedSource) {
                throw "运行中的采集监督器使用旧代码，请先从菜单停止采集后重新启动。"
            }
        }
        Write-Success "采集监督器已经在运行，不会重复启动"
        return
    }

    $shellPath = (Get-Process -Id $PID).Path
    $supervisorScript = Join-Path $PSScriptRoot "run_collector_supervisor.ps1"
    $env:RIVENSNIPER_PROJECT_ROOT = $ProjectRoot
    $env:RIVENSNIPER_UV_PATH = $UvPath
    Write-Step "在新窗口启动采集监督器"
    $arguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$supervisorScript`""
    )
    $process = Start-Process `
        -FilePath $shellPath `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -PassThru

    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Seconds 1
        if ($process.HasExited) {
            throw "采集监督器窗口提前退出，请查看该窗口中的错误信息。"
        }
        $status = Get-CollectorStatus -UvPath $UvPath
        if ($status.supervisor_running) {
            Write-Success "采集监督器已启动"
            return
        }
    }
    throw "等待监督器启动超时，请查看新窗口中的错误信息。"
}

function Get-InactiveSlots {
    param($Status)

    $result = New-Object System.Collections.Generic.List[string]
    foreach ($slot in $script:CollectorSlots) {
        $state = $Status.slots.PSObject.Properties[$slot].Value
        if (-not $state.process_alive -or
            $script:ActiveStatuses -notcontains [string]$state.status) {
            [void]$result.Add($slot)
        }
    }
    return @($result)
}

function Start-AutomaticTicketWizard {
    param([string]$UvPath)

    Write-Host ""
    Write-Host "全自动取票会逐槽打开 Warframe 启动器、自动点击开始游戏、填写本机加密保存的登录凭据并完成认证。" -ForegroundColor Yellow
    Write-Host "开始前请退出所有现有 Warframe 和 Warframe 启动器；脚本只会结束自己启动的进程。"
    Write-Step "为尚未运行的槽执行全自动登录取票"
    Push-Location $ProjectRoot
    try {
        & $UvPath run python `
            scripts/auto_capture_chat_tickets.py `
            --runtime-root $script:RuntimeRoot
        $ticketExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($ticketExitCode -eq 0) {
        Write-Success "全部待取票槽已完成"
    }
    else {
        Write-Host "部分槽未完成；请查看上方对应槽的原因，已成功的槽不受影响。" -ForegroundColor Red
    }
}

function Start-AutoLoginConfiguration {
    param([string]$UvPath)

    Write-Host ""
    Write-Host "将为当前模式各槽配置自动登录账号；密码输入时不会回显。" -ForegroundColor Yellow
    Push-Location $ProjectRoot
    try {
        & $UvPath run python `
            scripts/auto_capture_chat_tickets.py `
            --configure `
            --runtime-root $script:RuntimeRoot
        $configurationExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($configurationExitCode -eq 0) {
        Write-Success "自动登录凭据已更新"
    }
    else {
        Write-Host "自动登录凭据未能完成配置。" -ForegroundColor Red
    }
}

function Start-TicketWizard {
    param([string]$UvPath, $Accounts)

    $status = Get-CollectorStatus -UvPath $UvPath
    $slots = @(Get-InactiveSlots $status)
    if ($slots.Count -eq 0) {
        if ($script:ActiveCollectorMode -eq "4") {
            Write-Success "A/B/C/D 四槽都已运行，无需重新取票"
        }
        else {
            Write-Success "A-Q 十七槽都已运行，无需重新取票"
        }
        return
    }

    Write-Host ""
    Write-Host "接下来会逐个账号取票。每个槽都按同样步骤操作：" -ForegroundColor Yellow
    Write-Host "  1. 启动 Warframe，使用目标账号进入世界，并确认聊天可正常收发。"
    Write-Host "  2. 回到本窗口确认；程序会只读扫描内存并让对应槽验证候选。"
    Write-Host "  3. 认证成功后游戏会自动关闭；再换下一个账号。"
    Write-Host ""
    foreach ($slot in $slots) {
        $status = Get-CollectorStatus -UvPath $UvPath
        $state = $status.slots.PSObject.Properties[$slot].Value
        if ($state.process_alive -and $script:ActiveStatuses -contains [string]$state.status) {
            continue
        }
        $nick = [string]$Accounts.PSObject.Properties[$slot].Value.nick
        $slotLabel = if ($script:ActiveCollectorMode -eq "17") {
            "$slot / $($script:RegionalSlots[$slot])"
        }
        else {
            $slot
        }
        $answer = Read-Host "槽 $slotLabel（账号 $nick）准备好后输入 Y 开始；输入其他内容跳过"
        if ($answer.Trim().ToUpperInvariant() -ne "Y") {
            continue
        }
        Write-Step "为槽 $slot 捕获登录票据"
        Push-Location $ProjectRoot
        try {
            & $UvPath run python `
                scripts/capture_chat_ticket.py `
                --slot $slot `
                --nick $nick `
                --runtime-root $script:RuntimeRoot
            $ticketExitCode = $LASTEXITCODE
        }
        finally {
            Pop-Location
        }
        if ($ticketExitCode -eq 0) {
            Write-Success "槽 $slot 已完成认证"
        }
        else {
            Write-Host "槽 $slot 取票未成功；可稍后从菜单重试，不影响其他槽。" -ForegroundColor Red
        }
    }
}

function Start-ManualSnapshot {
    param([string]$UvPath)

    $status = Get-CollectorStatus -UvPath $UvPath
    $availableSlots = @(
        foreach ($slot in $script:CollectorSlots) {
            $state = $status.slots.PSObject.Properties[$slot].Value
            if ($state.process_alive -and [string]$state.status -in @(
                "joining", "listening", "degraded", "reconnecting"
            )) {
                $slot
            }
        }
    )
    if ($availableSlots.Count -eq 0) {
        Write-Host "当前没有可执行手动快照的运行中槽位。" -ForegroundColor Yellow
        return
    }

    Write-Host ""
    Write-Host (
        "可执行手动快照的槽：{0}" -f ($availableSlots -join "/")
    ) -ForegroundColor Yellow
    $slot = (Read-Host "输入槽位；直接按回车取消").Trim().ToUpperInvariant()
    if ([string]::IsNullOrWhiteSpace($slot)) {
        Write-Host "已取消手动快照。" -ForegroundColor DarkGray
        return
    }
    if ($slot -notin $script:CollectorSlots) {
        Write-Host "槽 $slot 不属于当前 $($script:ActiveCollectorMode) 槽模式。" -ForegroundColor Yellow
        return
    }
    if ($slot -notin $availableSlots) {
        Write-Host "槽 $slot 当前未运行，或尚未进入可接受快照的状态。" -ForegroundColor Yellow
        return
    }

    Write-Step "为槽 $slot 发起一次成员快照"
    Push-Location $ProjectRoot
    try {
        & $UvPath run --no-sync python scripts/run_chat_collector.py `
            snapshot --slot $slot --runtime-root $script:RuntimeRoot
        $snapshotExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($snapshotExitCode -ne 0) {
        Write-Host "槽 $slot 手动快照未能发起；请根据上方错误重试。" -ForegroundColor Red
        return
    }
    Write-Success "槽 $slot 手动快照请求已装载，将查询该槽分配的全部频道"
}

function Test-BotRuntimeRunning {
    $path = Join-Path $ProjectRoot ".runtime\bot_state.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $false
    }
    try {
        $state = Get-Content -LiteralPath $path -Raw |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        return $false
    }
    if ((Get-ObjectPropertyValue $state "status" "") -ne "running") {
        return $false
    }
    return Test-ProcessIdentity `
        -ProcessId ([int](Get-ObjectPropertyValue $state "pid" 0)) `
        -ExpectedIdentity ([string](Get-ObjectPropertyValue `
            $state "process_identity" ""))
}

function Test-IrcDeliveryStopped {
    param($Status)

    $control = Get-ObjectPropertyValue $Status "control" $null
    $delivery = Get-ObjectPropertyValue $Status "delivery" $null
    $controlRunId = [string](Get-ObjectPropertyValue $control "run_id" "")
    $deliveryRunId = [string](Get-ObjectPropertyValue $delivery "run_id" "")
    return (
        -not [string]::IsNullOrWhiteSpace($controlRunId) -and
        $deliveryRunId -eq $controlRunId -and
        -not [bool](Get-ObjectPropertyValue $delivery "accepting" $true) -and
        [int](Get-ObjectPropertyValue $delivery "irc_queued" -1) -eq 0 -and
        [int](Get-ObjectPropertyValue $delivery "irc_inflight" -1) -eq 0
    )
}

function Stop-Collector {
    param([string]$UvPath)

    Invoke-UvChecked `
        -UvPath $UvPath `
        -ProjectRoot $ProjectRoot `
        -Arguments @(
            "run", "--no-sync", "python", "scripts/run_chat_collector.py",
            "stop", "--runtime-root", $script:RuntimeRoot
        ) `
        -FailureMessage "停止请求发送失败"
    if ($script:ActiveCollectorMode -eq "4") {
        Write-Step "停止请求已发送，等待监督器与四槽进程完全退出"
    }
    else {
        Write-Step "停止请求已发送，等待监督器与十七槽进程完全退出"
    }
    for ($attempt = 0; $attempt -lt 25; $attempt++) {
        $status = Get-CollectorStatus -UvPath $UvPath
        $liveSlots = @(
            foreach ($slot in $script:CollectorSlots) {
                $state = $status.slots.PSObject.Properties[$slot].Value
                if ($state.process_alive) {
                    $slot
                }
            }
        )
        if (-not $status.supervisor_running -and $liveSlots.Count -eq 0) {
            if (Test-IrcDeliveryStopped $status) {
                Show-CollectorStatus $status
                Write-Success "采集已完全停止，尚未发送的 IRC 消息已撤销"
                return
            }
            if (-not (Test-BotRuntimeRunning)) {
                Show-CollectorStatus $status
                if ($script:ActiveCollectorMode -eq "4") {
                    Write-Success "监督器与四槽 worker 均已退出"
                }
                else {
                    Write-Success "监督器与十七槽 worker 均已退出"
                }
                Write-Host (
                    "未检测到正在运行的当前版 BOT，无法取得内存队列确认；" +
                    "如果 BOT 窗口仍开，请重启 BOT。"
                ) -ForegroundColor Yellow
                return
            }
            Write-Step "监督器已退出，等待 BOT 撤销尚未发送的 IRC 消息"
            for ($ackAttempt = 0; $ackAttempt -lt 10; $ackAttempt++) {
                $status = Get-CollectorStatus -UvPath $UvPath
                if (Test-IrcDeliveryStopped $status) {
                    Show-CollectorStatus $status
                    Write-Success "采集已完全停止，尚未发送的 IRC 消息已撤销"
                    return
                }
                Start-Sleep -Seconds 1
            }
            Show-CollectorStatus $status
            if ($script:ActiveCollectorMode -eq "4") {
                Write-Success "监督器与四槽 worker 均已退出"
            }
            else {
                Write-Success "监督器与十七槽 worker 均已退出"
            }
            Write-Host (
                "BOT 在 10 秒内未写入 IRC 撤销确认；请重启 BOT，" +
                "以确保旧内存队列不会继续发送。"
            ) -ForegroundColor Yellow
            return
        }
        Start-Sleep -Seconds 1
    }
    $status = Get-CollectorStatus -UvPath $UvPath
    Show-CollectorStatus $status
    throw "等待采集停止超时；请保留本窗口并检查仍显示运行中的槽。"
}

try {
    $requestedMode = $CollectorMode.Trim()
    if (-not [string]::IsNullOrWhiteSpace($requestedMode) -and
        $requestedMode -notin @("4", "17")) {
        throw "-CollectorMode 只能是 4 或 17。"
    }
    $initialMode = if (-not [string]::IsNullOrWhiteSpace($requestedMode)) {
        $requestedMode
    }
    else {
        Read-CollectorMode
    }
    Set-CollectorContext -Mode $initialMode
    $modeTitle = if ($initialMode -eq "4") { "四槽" } else { "十七槽" }
    Write-LauncherBanner `
        -Title "RivenSniper $modeTitle 聊天采集一键启动" `
        -Subtitle "自动初始化、启动监督器，并逐槽引导获取登录票据"

    $requiredFiles = @(
        "scripts/run_chat_collector.py",
        "scripts/capture_chat_ticket.py",
        "scripts/auto_capture_chat_tickets.py",
        "src/plugins/riven_sniper/chat_collector/windows_auto_login.py",
        "scripts/windows/run_collector_supervisor.ps1",
        "configs/chat_collector_shards.json",
        "configs/chat_collector_accounts.example.json",
        "pyproject.toml",
        "uv.lock",
        ".env.example"
    )
    if ($initialMode -eq "17") {
        $requiredFiles += @(
            "configs/chat_collector_shards_17.json",
            "configs/chat_collector_accounts_17.example.json"
        )
    }
    Test-RequiredFiles -ProjectRoot $ProjectRoot -RelativePaths $requiredFiles
    if ($Doctor) {
        $uv = $UvPath
        if ([string]::IsNullOrWhiteSpace($uv)) {
            $uv = Find-UvExecutable
        }
        if ([string]::IsNullOrWhiteSpace($uv)) {
            throw "启动脚本结构正常，但当前电脑没有找到 uv。"
        }
        if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) {
            throw "指定的 uv 不存在：$uv"
        }
        Write-Success "聊天采集启动脚本自检通过"
        Write-Host "项目目录：$ProjectRoot"
        Write-Host "uv：$uv"
        exit 0
    }

    $uv = $UvPath
    if ([string]::IsNullOrWhiteSpace($uv)) {
        $uv = Ensure-UvExecutable
    }
    elseif (-not (Test-Path -LiteralPath $uv -PathType Leaf)) {
        throw "指定的 uv 不存在：$uv"
    }
    Ensure-ProjectEnvironment -UvPath $uv -ProjectRoot $ProjectRoot
    Initialize-CollectorFiles -UvPath $uv
    if (-not [string]::IsNullOrWhiteSpace($requestedMode)) {
        Set-CollectorModeValue -UvPath $uv -Mode $requestedMode
    }
    else {
        Set-CollectorContext -Mode (Read-CollectorMode)
    }
    $accounts = Ensure-CollectorAccounts
    Ensure-CollectorPsk
    Enable-BotCollectorFeed
    Validate-Collector -UvPath $uv
    if ($PrepareOnly) {
        Write-Success "采集首次配置准备完成"
        exit 0
    }
    Start-CollectorSupervisor -UvPath $uv

    while ($true) {
        $status = Get-CollectorStatus -UvPath $uv
        Show-CollectorStatus $status
        Write-Host "1. 全自动为尚未运行的槽登录取票（推荐）"
        Write-Host "2. 刷新状态"
        Write-Host "3. 打开账号配置文件"
        Write-Host "4. 正常停止全部采集"
        Write-Host "5. 停止后切换 4/17 槽模式"
        Write-Host "6. 手动执行一次成员快照"
        Write-Host "7. 手动为尚未运行的槽逐个取票"
        Write-Host "8. 配置或更新全自动登录凭据"
        Write-Host "0. 关闭本向导（采集继续运行）"
        $choice = (Read-Host "请选择").Trim()
        switch ($choice) {
            "1" {
                Start-AutomaticTicketWizard -UvPath $uv
            }
            "2" {
                continue
            }
            "3" {
                Start-Process notepad.exe -ArgumentList @(
                    "`"$script:AccountsPath`""
                )
                Write-Host "修改账号前请先停止采集；修改后重新运行本启动器。" -ForegroundColor Yellow
            }
            "4" {
                Stop-Collector -UvPath $uv
                break
            }
            "5" {
                Stop-Collector -UvPath $uv
                while ($true) {
                    $newMode = (Read-Host "输入新的采集模式（4 或 17）").Trim()
                    if ($newMode -in @("4", "17")) {
                        break
                    }
                    Write-Host "请输入 4 或 17。" -ForegroundColor Yellow
                }
                Set-CollectorModeValue -UvPath $uv -Mode $newMode
                $accounts = Ensure-CollectorAccounts
                Validate-Collector -UvPath $uv
                Start-CollectorSupervisor -UvPath $uv
            }
            "6" {
                Start-ManualSnapshot -UvPath $uv
            }
            "7" {
                Start-TicketWizard -UvPath $uv -Accounts $accounts
            }
            "8" {
                Start-AutoLoginConfiguration -UvPath $uv
            }
            "0" {
                Write-Host "向导已关闭；采集监督器仍在独立窗口运行。" -ForegroundColor Green
                break
            }
            default {
                Write-Host "请输入 0、1、2、3、4、5、6、7 或 8。" -ForegroundColor Yellow
                continue
            }
        }
        if ($choice -eq "0" -or $choice -eq "4") {
            break
        }
    }
    exit 0
}
catch {
    Write-Host ""
    Write-Host "[启动失败] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "如果无法判断原因，请截图本窗口和监督器窗口的完整内容。" -ForegroundColor Yellow
    exit 1
}
