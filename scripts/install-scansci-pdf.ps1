[CmdletBinding()]
param(
    [string]$PluginDir = (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'plugins\scansci-pdf'),
    [string]$MarketplacePath = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.agents\plugins\marketplace.json'),
    [string]$Repo = 'https://github.com/Rimagination/scansci-pdf.git',
    [string]$Branch = 'main',
    [switch]$SkipPull,
    [switch]$SkipCodexAdd
)

$ErrorActionPreference = 'Stop'

function Write-Step {
    param([string]$Message)
    Write-Host "[ScanSci PDF] $Message" -ForegroundColor Cyan
}

function Ensure-Parent {
    param([string]$Path)
    $parent = Split-Path -Parent -Path $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
}

function Save-Json {
    param(
        [string]$Path,
        [object]$Value
    )

    Ensure-Parent $Path
    $json = $Value | ConvertTo-Json -Depth 10
    $utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $utf8NoBom)
}

function Invoke-Native {
    param(
        [string]$Command,
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令失败（$LASTEXITCODE）：$Command $($Arguments -join ' ')"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw '未找到 git，请先安装 Git。'
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw '未找到 python，请先安装 Python 3。'
}

$resolvedPluginDir = [System.IO.Path]::GetFullPath($PluginDir)
$gitDir = Join-Path $resolvedPluginDir '.git'

if (Test-Path -LiteralPath $resolvedPluginDir) {
    if (-not (Test-Path -LiteralPath $gitDir)) {
        throw "插件目录已存在但不是 Git 仓库：$resolvedPluginDir"
    }

    if (-not $SkipPull) {
        Write-Step "更新仓库：$resolvedPluginDir"
        Push-Location $resolvedPluginDir
        try {
            Invoke-Native 'git' @('fetch', 'origin', $Branch)
            Invoke-Native 'git' @('checkout', $Branch)
            Invoke-Native 'git' @('pull', '--ff-only', 'origin', $Branch)
        }
        finally {
            Pop-Location
        }
    }
}
else {
    Write-Step "克隆仓库：$Repo"
    Ensure-Parent $resolvedPluginDir
    Invoke-Native 'git' @('clone', '--branch', $Branch, '--single-branch', $Repo, $resolvedPluginDir)
}

$manifestPath = Join-Path $resolvedPluginDir '.codex-plugin\plugin.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "未找到 Codex 插件清单：$manifestPath"
}

Write-Step '以 editable 模式安装 scansci-pdf CLI'
Invoke-Native 'python' @('-m', 'pip', 'install', '-e', $resolvedPluginDir)

if (Test-Path -LiteralPath $MarketplacePath) {
    $marketplaceText = [System.IO.File]::ReadAllText($MarketplacePath)
    $marketplace = $marketplaceText | ConvertFrom-Json
}
else {
    $marketplace = [pscustomobject]@{
        name = 'local-plugins'
        plugins = @()
    }
}

if ([string]::IsNullOrWhiteSpace([string]$marketplace.name)) {
    $marketplace | Add-Member -NotePropertyName name -NotePropertyValue 'local-plugins'
}

$marketplacePlugins = if ($null -eq $marketplace.plugins) { @() } else { @($marketplace.plugins) }
$marketplacePlugins = @($marketplacePlugins | Where-Object { $_.name -ne 'scansci-pdf' })
$marketplacePlugins += [pscustomobject]@{
    name = 'scansci-pdf'
    source = [pscustomobject]@{
        source = 'local'
        path = './plugins/scansci-pdf'
    }
    policy = [pscustomobject]@{
        installation = 'AVAILABLE'
        authentication = 'ON_INSTALL'
    }
    category = 'Science'
}

if ($null -eq $marketplace.PSObject.Properties['plugins']) {
    $marketplace | Add-Member -NotePropertyName plugins -NotePropertyValue $marketplacePlugins
}
else {
    $marketplace.plugins = $marketplacePlugins
}

Write-Step "更新个人 marketplace：$MarketplacePath"
Save-Json $MarketplacePath $marketplace

if (-not $SkipCodexAdd) {
    $codexCommand = Get-Command codex -ErrorAction SilentlyContinue
    if ($null -eq $codexCommand) {
        Write-Warning '未找到 codex CLI；请在 Codex App 的插件管理页刷新个人 marketplace。'
    }
    else {
        Write-Step "注册插件：scansci-pdf@$($marketplace.name)"
        & $codexCommand.Source plugin add "scansci-pdf@$($marketplace.name)"
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'codex plugin add 未成功；请在 Codex App 的插件管理页刷新个人 marketplace。'
        }
    }
}

Write-Host ''
Write-Host 'ScanSci PDF Codex 插件已准备完成。' -ForegroundColor Green
Write-Host "插件目录：$resolvedPluginDir"
