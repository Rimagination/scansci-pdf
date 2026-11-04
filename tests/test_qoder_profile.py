"""Compliance profile tests: the Qoder edition must never reach grey sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scansci_pdf import config as config_mod
from scansci_pdf import sources


@pytest.fixture
def qoder_profile(monkeypatch):
    monkeypatch.setenv("SCANSCI_PDF_PROFILE", "qoder")


def test_qoder_profile_forces_legal_only(qoder_profile):
    cfg = config_mod.load_config()

    assert cfg["profile"] == "qoder"
    assert cfg["scihub_enabled"] is False
    assert cfg["download_strategy"] == "legal_only"


def test_qoder_profile_locks_config_keys(qoder_profile):
    with pytest.raises(ValueError, match="locked by the 'qoder' compliance profile"):
        config_mod.update_config("scihub_enabled", "true")

    with pytest.raises(ValueError, match="locked by the 'qoder' compliance profile"):
        config_mod.update_config("download_strategy", "scihub_only")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scihub_enabled": True},
        {"strategy": "scihub_only"},
        {"strategy": "grey_only"},
        {"strategy": "scihub_first"},
    ],
)
def test_qoder_profile_rejects_explicit_grey(qoder_profile, kwargs):
    result = sources.download("10.1234/qoder-profile-probe", **kwargs)

    assert result["success"] is False
    assert result.get("error_type") == "compliance_profile"
    assert "legal-only" in result.get("reason", "")


def test_qoder_profile_builds_no_grey_sources(qoder_profile):
    cfg = config_mod.load_config()
    built = sources._build_free_sources("10.1038/nature14236", cfg)
    labels = {label for _, label in built}

    assert not labels & {"Sci-Hub", "SciBban", "LibGen"}


def test_qoder_profile_not_persisted(qoder_profile, monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    config_mod.save_config(config_mod.load_config())

    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "profile" not in saved


def test_qoder_connector_uses_profile():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "qoder-plugin" / ".mcp.json").read_text(encoding="utf-8"))

    args = data["mcpServers"]["scansci-pdf"]["args"]
    assert args == ["run", "--profile", "qoder"]


def test_default_profile_keeps_grey_available():
    # The open-source package keeps the capability; only the Qoder profile
    # turns it off. This guards against accidentally flipping the default.
    assert config_mod.DEFAULT_CONFIG["scihub_enabled"] is True
    assert config_mod.DEFAULT_CONFIG["download_strategy"] == "fastest"
