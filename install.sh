#!/bin/bash
# ScanSci PDF - Quick Install Script
set -e

echo "=== ScanSci PDF Installer ==="
echo ""

# Check Python
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "ERROR: Python not found. Install Python 3.11+ first."
    echo "  Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv"
    echo "  macOS: brew install python@3.12"
    exit 1
fi

PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Found Python $PY_VERSION"

# Check version
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]); then
    echo "ERROR: Python 3.11+ required, found $PY_VERSION"
    exit 1
fi

# Create virtual environment
VENV_DIR="${SCANSCI_PDF_VENV:-$HOME/.scansci-pdf/venv}"
echo "Creating virtual environment at $VENV_DIR ..."
mkdir -p "$(dirname "$VENV_DIR")"
$PYTHON -m venv "$VENV_DIR"

# Activate and install
source "$VENV_DIR/bin/activate"
echo "Installing scansci-pdf with recommended optional dependencies (fast + vpnsci) ..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip -q
"$VENV_DIR/bin/python" -m pip install ".[fast,vpnsci]" -q

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Usage:"
echo "  # Activate virtual environment"
echo "  source $VENV_DIR/bin/activate"
echo ""
echo "  # Run as stdio MCP server (for Claude Code)"
echo "  scansci-pdf run --mode stdio"
echo ""
echo "  # Run as HTTP server"
echo "  scansci-pdf run --mode streamable_http"
echo ""
echo "  # Check dependencies"
echo "  scansci-pdf check"
echo ""
echo "  # Or use Docker (recommended)"
echo "  docker compose up -d"
echo ""

# Check dependencies
"$VENV_DIR/bin/scansci-pdf" check
