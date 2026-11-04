"""The marketplace ZIP must contain only the runtime payload."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "qoder-plugin" / "scripts" / "build-bundle.py"


def _build(tmp_path: Path, *extra: str) -> set[str]:
    out = tmp_path / "qoder.zip"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(out), *extra],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    with zipfile.ZipFile(out) as zf:
        return set(zf.namelist())


def test_bundle_has_wrapped_runtime_payload(tmp_path):
    names = _build(tmp_path)

    assert "scansci-pdf/.mcp.json" in names
    assert "scansci-pdf/.qoder-plugin/plugin.json" in names
    assert "scansci-pdf/README.md" in names
    assert "scansci-pdf/CONNECTORS.md" in names
    assert "scansci-pdf/LICENSE" in names
    assert "scansci-pdf/icon.png" in names
    assert "scansci-pdf/skills/scansci-pdf/SKILL.md" in names
    for command in ("batch", "doctor", "paper"):
        assert f"scansci-pdf/commands/scansci/{command}.md" in names


def test_bundle_excludes_development_and_grey_surface(tmp_path):
    names = _build(tmp_path)

    assert not any("scripts/" in name for name in names)
    assert not any(name.endswith("RELEASE.md") for name in names)
    assert not any("references/" in name for name in names)
    assert not any("scansci-sort" in name for name in names)
    # The wrapped layout is the one that installed successfully; a flat root is
    # available only through --flat for installer testing.
    assert not any(name.startswith(".qoder-plugin/") for name in names)


def test_bundle_flat_layout_is_available_for_installer_tests(tmp_path):
    names = _build(tmp_path, "--flat")

    assert ".mcp.json" in names
    assert ".qoder-plugin/plugin.json" in names
    assert not any(name.startswith("scansci-pdf/") for name in names)
