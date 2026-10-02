param(
    [string]$ProjectRoot = $env:RIVENSNIPER_PROJECT_ROOT,
    [string]$UvPath = $env:RIVENSNIPER_UV_PATH
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "launcher_common.ps1")

if ([string]::IsNullOrWhiteSpace($ProjectRoot) -or
    [string]::IsNullOrWhiteSpace($UvPath)) {
    throw "监督器启动参数不完整；请从「启动聊天采集.cmd」进入。"
}

$host.UI.RawUI.WindowTitle = "RivenSniper 聊天采集监督器（请勿直接关闭）"
$runtimeRoot = Join-Path $ProjectRoot ".runtime\chat_collector"
$modePath = Join-Path $runtimeRoot "collector_mode.json"
$collectorMode = "4"
if (Test-Path -LiteralPath $modePath -PathType Leaf) {
    try {
        $modeDocument = Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
        if ([string]$modeDocument.mode -eq "17") {
            $collectorMode = "17"
        }
    }
    catch {
        throw "采集模式文件无法读取：$modePath"
    }
}
$modeTitle = if ($collectorMode -eq "4") { "四槽" } else { "十七槽" }
Write-LauncherBanner `
    -Title "RivenSniper $modeTitle 聊天采集监督器" `
    -Subtitle "请保持本窗口打开；正常停止请使用采集启动器中的停止菜单"

try {
    Push-Location $ProjectRoot
    try {
        & $UvPath run --no-sync python scripts/run_chat_collector.py `
            run --runtime-root $runtimeRoot
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw "采集监督器异常退出（退出码 $exitCode）。"
    }
    Write-Success "采集监督器已正常停止"
}
catch {
    Write-Host "[监督器失败] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "请截图本窗口的完整内容。" -ForegroundColor Yellow
}

Wait-ForUser "按回车键关闭监督器窗口"
