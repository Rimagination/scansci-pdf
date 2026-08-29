"""The cloakbrowser floor is a hard gate: stale stealth kernels get refused."""

from __future__ import annotations

import pytest

from scansci_pdf import browser_backend as bb


def test_gate_passes_on_current(monkeypatch):
    monkeypatch.setattr(bb, "_cloakbrowser_dist_version", lambda: (0, 5, 9))
    bb._enforce_cloakbrowser_floor()  # no raise


def test_gate_blocks_old_with_upgrade_hint(monkeypatch):
    monkeypatch.setattr(bb, "_cloakbrowser_dist_version", lambda: (0, 4, 11))
    with pytest.raises(RuntimeError, match="pip install -U cloakbrowser"):
        bb._enforce_cloakbrowser_floor()


def test_gate_bypass_env(monkeypatch):
    monkeypatch.setattr(bb, "_cloakbrowser_dist_version", lambda: (0, 4, 11))
    monkeypatch.setenv("SCANSCI_ALLOW_OLD_CLOAKBROWSER", "1")
    bb._enforce_cloakbrowser_floor()


def test_gate_skips_when_distribution_missing(monkeypatch):
    monkeypatch.setattr(bb, "_cloakbrowser_dist_version", lambda: None)
    bb._enforce_cloakbrowser_floor()  # plain-python/importless edge: no gate


def test_launch_refuses_old_version(monkeypatch):
    calls = []
    monkeypatch.setattr(bb, "_cloakbrowser_dist_version", lambda: (0, 4, 11))
    import cloakbrowser

    orig = cloakbrowser.launch
    monkeypatch.setattr(cloakbrowser, "launch", lambda **kw: calls.append(kw))
    with pytest.raises(RuntimeError):
        bb._launch_cloakbrowser(headless=True, proxy=None, args=None, humanize=True)
    assert calls == []  # never reached the real launcher
