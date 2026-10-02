param(
    [string]$ProjectRoot = "",
    [string]$UvPath = "",
    [switch]$Doctor
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

function Ensure-BotConfiguration {
    param(
        [string]$Root,
        [string]$UvPath
    )

    $envPath = Join-Path $Root ".env"
    $examplePath = Join-Path $Root ".env.example"
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        Copy-Item -LiteralPath $examplePath -Destination $envPath
        Write-Success "已根据模板创建 .env 配置文件"
    }

    $tokenValue = Get-UnquotedValue (Read-DotEnvValue -Path $envPath -Key "ONEBOT_ACCESS_TOKEN")
    $apiRootsValue = Get-UnquotedValue (Read-DotEnvValue -Path $envPath -Key "ONEBOT_API_ROOTS")
    Push-Location $Root
    try {
        & $UvPath run --no-sync python scripts/configure_target.py has-qq
        $targetCheckExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($targetCheckExitCode -notin @(0, 1)) {
        throw "无法读取 Bot 目标配置（退出码 $targetCheckExitCode）。"
    }
    $hasQQTarget = $targetCheckExitCode -eq 0
    $needsWizard = (
        -not $hasQQTarget -or
        [string]::IsNullOrWhiteSpace($tokenValue) -or
        $tokenValue -eq "改成随机字符串" -or
        [string]::IsNullOrWhiteSpace($apiRootsValue)
    )

    if ($needsWizard) {
        if (-not $hasQQTarget) {
            Write-Host "首次启动需要创建一个单所有者 QQ 群目标。" -ForegroundColor Yellow
            while ($true) {
                $groupInput = Read-Host "接收推送的 QQ 群号"
                $groupId = 0L
                if ([long]::TryParse($groupInput, [ref]$groupId) -and $groupId -gt 0) {
                    break
                }
                Write-Host "QQ群号必须是正整数。" -ForegroundColor Red
            }
            while ($true) {
                $ownerInput = Read-Host "该群唯一所有者的 QQ 号"
                $ownerQQ = 0L
                if ([long]::TryParse($ownerInput, [ref]$ownerQQ) -and $ownerQQ -gt 0) {
                    break
                }
                Write-Host "所有者 QQ 必须是正整数。" -ForegroundColor Red
            }
            Push-Location $Root
            try {
                & $UvPath run --no-sync python scripts/configure_target.py `
                    upsert-qq $groupId $ownerQQ
                if ($LASTEXITCODE -ne 0) {
                    throw "创建 QQ 群目标失败（退出码 $LASTEXITCODE）。"
                }
            }
            finally {
                Pop-Location
            }
            Write-Success "QQ 群目标已创建"
        }
        if ([string]::IsNullOrWhiteSpace($tokenValue) -or $tokenValue -eq "改成随机字符串") {
            $tokenValue = New-AccessToken
            Write-DotEnvValue -Path $envPath -Key "ONEBOT_ACCESS_TOKEN" -Value $tokenValue
        }
        if ([string]::IsNullOrWhiteSpace($apiRootsValue)) {
            $apiRootsValue = '{"*":"http://127.0.0.1:3000/"}'
            Write-DotEnvValue -Path $envPath -Key "ONEBOT_API_ROOTS" -Value $apiRootsValue
        }
        Write-Success "BOT 基础配置已保存"
    }

    $port = Get-UnquotedValue (Read-DotEnvValue -Path $envPath -Key "PORT")
    $portNumber = 0
    if (-not [int]::TryParse($port, [ref]$portNumber) -or
        $portNumber -lt 1 -or $portNumber -gt 65535) {
        $port = "8180"
        Write-DotEnvValue -Path $envPath -Key "PORT" -Value $port
    }
    $tokenValue = Get-UnquotedValue (Read-DotEnvValue -Path $envPath -Key "ONEBOT_ACCESS_TOKEN")
    return [pscustomobject]@{
        Port = $port
        Token = $tokenValue
        ApiRoots = $apiRootsValue
        WasConfigured = $needsWizard
    }
}

Write-LauncherBanner `
    -Title "RivenSniper QQ BOT 一键启动" `
    -Subtitle "首次运行会自动准备环境并引导完成基础配置"

try {
    Test-RequiredFiles -ProjectRoot $ProjectRoot -RelativePaths @(
        "bot.py",
        "pyproject.toml",
        "uv.lock",
        ".env.example"
    )
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
        Write-Success "BOT 启动脚本自检通过"
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
    $configuration = Ensure-BotConfiguration -Root $ProjectRoot -UvPath $uv

    $botStatePath = Join-Path $ProjectRoot ".runtime\bot_state.json"
    if (Test-Path -LiteralPath $botStatePath -PathType Leaf) {
        $botState = $null
        try {
            $botState = Get-Content -LiteralPath $botStatePath -Raw |
                ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            Write-Host "旧 BOT 状态文件无法解析，将继续执行启动检查。" -ForegroundColor Yellow
        }
        $stateStatus = Get-ObjectPropertyValue $botState "status" ""
        $statePid = [int](Get-ObjectPropertyValue $botState "pid" 0)
        $stateIdentity = [string](
            Get-ObjectPropertyValue $botState "process_identity" "")
        if ($stateStatus -eq "running" -and
            (Test-ProcessIdentity `
                -ProcessId $statePid `
                -ExpectedIdentity $stateIdentity)) {
            $stateStartedAt = Get-ObjectPropertyValue `
                $botState "started_at" "unknown"
            $stateGitCommit = Get-ObjectPropertyValue `
                $botState "git_commit" "unknown"
            Write-Host (
                "检测到 BOT 已运行：PID {0} / 启动 {1} / Git {2}" -f `
                $statePid, $stateStartedAt, $stateGitCommit
            ) -ForegroundColor Yellow
            $loadedSourceValue = [string](Get-ObjectPropertyValue `
                $botState "source_mtime_utc" "")
            if (-not [string]::IsNullOrWhiteSpace($loadedSourceValue)) {
                $loadedSource = [datetime]::Parse(
                    $loadedSourceValue).ToUniversalTime()
                $currentSource = Get-ProjectSourceTimestamp -ProjectRoot $ProjectRoot
                if ($currentSource -gt $loadedSource) {
                    throw "运行中的 BOT 使用旧代码。请先关闭旧 BOT 窗口，再重新双击启动。"
                }
            }
            throw "BOT 已经运行，无需重复启动。"
        }
    }

    Write-Host ""
    Write-Host "请在 SnowLuma 中配置 OneBot v11：" -ForegroundColor Yellow
    Write-Host "  地址：ws://127.0.0.1:$($configuration.Port)/onebot/v11/ws" -ForegroundColor White
    Write-Host "  Token：$($configuration.Token)" -ForegroundColor White
    Write-Host "  HTTP API：$($configuration.ApiRoots)（启用对应 HTTP Server，使用相同 Token）" -ForegroundColor White
    if ($configuration.WasConfigured) {
        Wait-ForUser "完成协议端配置后按回车键启动 BOT"
    }

    Write-Step "启动 BOT；保持本窗口打开，按 Ctrl+C 可停止"
    $botExitCode = 0
    Push-Location $ProjectRoot
    try {
        & $uv run --no-sync python bot.py
        $botExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($botExitCode -ne 0) {
        throw "BOT 已异常退出（退出码 $botExitCode）。请保留上方错误信息。"
    }
    Write-Success "BOT 已停止"
    exit 0
}
catch {
    Write-Host ""
    Write-Host "[启动失败] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "如果无法判断原因，请截图本窗口的完整内容。" -ForegroundColor Yellow
    exit 1
}
