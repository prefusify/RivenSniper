Set-StrictMode -Version 2.0

function Write-LauncherBanner {
    param([string]$Title, [string]$Subtitle)

    try {
        Clear-Host
    }
    catch {
        # 重定向输出的自检环境没有控制台句柄，不影响启动逻辑。
    }
    Write-Host "============================================================" -ForegroundColor DarkCyan
    Write-Host "  $Title" -ForegroundColor Cyan
    Write-Host "  $Subtitle" -ForegroundColor Gray
    Write-Host "============================================================" -ForegroundColor DarkCyan
    Write-Host ""
}

function Write-Step {
    param([string]$Message)
    Write-Host "[进行中] $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "[完成] $Message" -ForegroundColor Green
}

function Wait-ForUser {
    param([string]$Message = "按回车键继续")
    [void](Read-Host $Message)
}

function Test-RequiredFiles {
    param([string]$ProjectRoot, [string[]]$RelativePaths)

    foreach ($relativePath in $RelativePaths) {
        $path = Join-Path $ProjectRoot $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "项目文件不完整，缺少：$relativePath。请重新解压或重新获取完整项目。"
        }
    }
}

function Find-UvExecutable {
    $command = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        $command = Get-Command uv -ErrorAction SilentlyContinue
    }
    if ($null -ne $command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Ensure-UvExecutable {
    $uv = Find-UvExecutable
    if ($null -ne $uv) {
        return $uv
    }

    Write-Host "没有检测到项目运行器 uv。" -ForegroundColor Yellow
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($null -eq $winget) {
        throw (
            "电脑上没有 uv，也没有 Windows 软件安装器 winget。" +
            "请先从 Microsoft Store 更新「应用安装程序」，再重新双击本脚本。"
        )
    }

    $answer = Read-Host "是否现在自动安装 uv？输入 Y 安装，输入其他内容取消"
    if ($answer.Trim().ToUpperInvariant() -ne "Y") {
        throw "已取消安装，项目尚未启动。"
    }

    Write-Step "通过 Windows 软件安装器安装 uv"
    & $winget.Source install --id astral-sh.uv --exact `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "uv 自动安装失败。请检查网络或 Microsoft Store 后重试。"
    }

    $env:Path = @(
        (Join-Path $env:USERPROFILE ".local\bin"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"),
        $env:Path
    ) -join ";"
    $uv = Find-UvExecutable
    if ($null -eq $uv) {
        throw "uv 已安装，但当前窗口还无法找到它。请关闭本窗口后重新双击启动脚本。"
    }
    Write-Success "uv 安装完成"
    return $uv
}

function Invoke-UvChecked {
    param(
        [string]$UvPath,
        [string]$ProjectRoot,
        [string[]]$Arguments,
        [string]$FailureMessage
    )

    Push-Location $ProjectRoot
    try {
        & $UvPath @Arguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw "$FailureMessage（退出码 $exitCode）"
    }
}

function Ensure-ProjectEnvironment {
    param([string]$UvPath, [string]$ProjectRoot)

    Write-Step "检查 Python 3.12 和项目依赖（首次运行会自动下载）"
    Invoke-UvChecked `
        -UvPath $UvPath `
        -ProjectRoot $ProjectRoot `
        -Arguments @("sync", "--locked") `
        -FailureMessage "项目依赖安装失败，请检查网络后重试"
    Write-Success "运行环境可用"
}

function Read-DotEnvValue {
    param([string]$Path, [string]$Key)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*=\s*(.*)$"
    $value = $null
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $match = [regex]::Match($line, $pattern)
        if ($match.Success) {
            $value = $match.Groups[1].Value.Trim()
        }
    }
    return $value
}

function Write-DotEnvValue {
    param([string]$Path, [string]$Key, [string]$Value)

    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
            [void]$lines.Add($line)
        }
    }

    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*="
    $replacement = "$Key=$Value"
    $replaced = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ([regex]::IsMatch($lines[$index], $pattern)) {
            $lines[$index] = $replacement
            $replaced = $true
        }
    }
    if (-not $replaced) {
        [void]$lines.Add($replacement)
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $lines, $encoding)
}

function Write-DotEnvDefault {
    param([string]$Path, [string]$Key, [string]$Value)

    $existing = Read-DotEnvValue -Path $Path -Key $Key
    if ([string]::IsNullOrWhiteSpace($existing)) {
        Write-DotEnvValue -Path $Path -Key $Key -Value $Value
    }
}

function Convert-ToQQJsonList {
    param([string]$Value, [string]$Label)

    $parts = @(
        $Value -split '[,，\s]+' |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -ne "" }
    )
    if ($parts.Count -eq 0) {
        throw "$Label 不能为空。"
    }
    foreach ($part in $parts) {
        if ($part -notmatch '^\d{5,20}$') {
            throw "$Label 中的「$part」不是有效数字；多个号码请用逗号分隔。"
        }
    }
    return "[" + ($parts -join ",") + "]"
}

function Test-QQJsonList {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '你的') {
        return $false
    }
    $trimmed = $Value.Trim()
    if (-not ($trimmed.StartsWith("[") -and $trimmed.EndsWith("]"))) {
        return $false
    }
    try {
        $items = @($trimmed | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        return $false
    }
    if ($items.Count -eq 0) {
        return $false
    }
    foreach ($item in $items) {
        if (([string]$item) -notmatch '^\d{5,20}$') {
            return $false
        }
    }
    return $true
}

function New-AccessToken {
    $bytes = New-Object byte[] 24
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Get-UnquotedValue {
    param([string]$Value)
    if ($null -eq $Value) {
        return ""
    }
    return $Value.Trim().Trim('"').Trim("'")
}

function Get-ObjectPropertyValue {
    param(
        $Object,
        [string]$Name,
        $DefaultValue = $null
    )

    if ($null -eq $Object) {
        return $DefaultValue
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $DefaultValue
    }
    return $property.Value
}

function Get-ProjectSourceTimestamp {
    param([string]$ProjectRoot)

    $paths = @(
        (Join-Path $ProjectRoot "bot.py"),
        (Join-Path $ProjectRoot "pyproject.toml")
    )
    foreach ($rootName in @("src", "scripts")) {
        $root = Join-Path $ProjectRoot $rootName
        if (Test-Path -LiteralPath $root -PathType Container) {
            $paths += @(
                Get-ChildItem -LiteralPath $root -Recurse -File |
                    Where-Object { $_.FullName -notmatch '\\__pycache__\\' } |
                    ForEach-Object { $_.FullName }
            )
        }
    }
    $latest = [datetime]::MinValue
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $modified = (Get-Item -LiteralPath $path).LastWriteTimeUtc
            if ($modified -gt $latest) {
                $latest = $modified
            }
        }
    }
    return $latest
}

function Test-ProcessIdentity {
    param(
        [int]$ProcessId,
        [string]$ExpectedIdentity
    )

    if ($ProcessId -le 0 -or [string]::IsNullOrWhiteSpace($ExpectedIdentity)) {
        return $false
    }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    try {
        $actual = $process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString("x16")
    }
    catch {
        return $false
    }
    return $actual -eq $ExpectedIdentity.Trim().ToLowerInvariant()
}
