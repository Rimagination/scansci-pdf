[CmdletBinding()]
param(
    [string]$PluginDir = (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'plugins\scansci-pdf'),
    [string]$MarketplacePath = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.agents\plugins\marketplace.json'),
    [string]$Repo = 'https://github.com/Rimagination/scansci-pdf.git',
    [string]$Branch = 'main',
    [switch]$SkipPull,
    [switch]$SkipInstall,
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
        throw "Command failed (exit code $LASTEXITCODE): $Command $($Arguments -join ' ')"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git was not found. Please install Git first.'
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'python was not found. Please install Python 3 first.'
}

$resolvedPluginDir = [System.IO.Path]::GetFullPath($PluginDir)
$gitDir = Join-Path $resolvedPluginDir '.git'

if (Test-Path -LiteralPath $resolvedPluginDir) {
    if (-not (Test-Path -LiteralPath $gitDir)) {
        throw "The plugin directory exists but is not a Git repository: $resolvedPluginDir"
    }

    if (-not $SkipPull) {
        Write-Step "Updating repository: $resolvedPluginDir"
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
    Write-Step "Cloning repository: $Repo"
    Ensure-Parent $resolvedPluginDir
    Invoke-Native 'git' @('clone', '--branch', $Branch, '--single-branch', $Repo, $resolvedPluginDir)
}

$manifestPath = Join-Path $resolvedPluginDir '.codex-plugin\plugin.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "Codex plugin manifest was not found: $manifestPath"
}

if (-not $SkipInstall) {
    Write-Step 'Installing the scansci-pdf CLI in editable mode for the current user'
    Invoke-Native 'python' @('-m', 'pip', 'install', '--user', '-e', $resolvedPluginDir)
}
else {
    Write-Step 'Skipping CLI installation because it is already installed'
}

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

Write-Step "Updating personal marketplace: $MarketplacePath"
Save-Json $MarketplacePath $marketplace

if (-not $SkipCodexAdd) {
    $codexCommand = Get-Command codex -ErrorAction SilentlyContinue
    if ($null -eq $codexCommand) {
        Write-Warning 'codex CLI was not found. Refresh the personal marketplace in Codex App.'
    }
    else {
        Write-Step "Registering plugin: scansci-pdf@$($marketplace.name)"
        try {
            & $codexCommand.Source plugin add "scansci-pdf@$($marketplace.name)"
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'codex plugin add failed. Refresh the personal marketplace in Codex App.'
            }
        }
        catch {
            Write-Warning ('Could not execute codex CLI: ' + $_.Exception.Message)
            Write-Warning 'Refresh the personal marketplace in Codex App.'
        }
    }
}

Write-Host ''
Write-Host 'ScanSci PDF Codex plugin is ready.' -ForegroundColor Green
Write-Host "Plugin directory: $resolvedPluginDir"
