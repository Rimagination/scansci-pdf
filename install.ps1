# ScanSci PDF - Quick Install Script (Windows)
$ErrorActionPreference = "Stop"

Write-Host "=== ScanSci PDF Installer ===" -ForegroundColor Cyan
Write-Host ""

# Check Python
$python = $null
if (Get-Command python3 -ErrorAction SilentlyContinue) {
    $python = "python3"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
} else {
    Write-Host "ERROR: Python not found. Install Python 3.11+ first." -ForegroundColor Red
    Write-Host "  Download: https://www.python.org/downloads/"
    Write-Host "  Or: winget install Python.Python.3.12"
    exit 1
}

$pyVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "Found Python $pyVersion"

# Check version
$pyMajor = & $python -c "import sys; print(sys.version_info.major)"
$pyMinor = & $python -c "import sys; print(sys.version_info.minor)"
if ([int]$pyMajor -lt 3 -or ([int]$pyMajor -eq 3 -and [int]$pyMinor -lt 11)) {
    Write-Host "ERROR: Python 3.11+ required, found $pyVersion" -ForegroundColor Red
    exit 1
}

# Create virtual environment
$venvDir = if ($env:SCANSCI_PDF_VENV) { $env:SCANSCI_PDF_VENV } else { "$env:USERPROFILE\.scansci-pdf\venv" }
Write-Host "Creating virtual environment at $venvDir ..."
New-Item -ItemType Directory -Force -Path (Split-Path $venvDir) | Out-Null
& $python -m venv $venvDir

# Activate and install
& "$venvDir\Scripts\Activate.ps1"
Write-Host "Installing scansci-pdf with recommended optional dependencies (fast + vpnsci) ..."
& "$venvDir\Scripts\python.exe" -m pip install --upgrade pip -q
& "$venvDir\Scripts\python.exe" -m pip install ".[fast,vpnsci]" -q

Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Usage:"
Write-Host "  # Activate virtual environment"
Write-Host "  & `"$venvDir\Scripts\Activate.ps1`""
Write-Host ""
Write-Host "  # Run as stdio MCP server (for Claude Code)"
Write-Host "  scansci-pdf run --mode stdio"
Write-Host ""
Write-Host "  # Run as HTTP server"
Write-Host "  scansci-pdf run --mode streamable_http"
Write-Host ""
Write-Host "  # Check dependencies"
Write-Host "  scansci-pdf check"
Write-Host ""
Write-Host "  # Or use Docker (recommended)"
Write-Host "  docker compose up -d"
Write-Host ""

# Check dependencies
& "$venvDir\Scripts\scansci-pdf.exe" check
