"""Camoufox backend selection: explicit config wins, unknown resets, args dropped."""

from __future__ import annotations

import pytest

import scansci_pdf.browser_backend as bb


@pytest.fixture
def _all_backends(monkeypatch):
    """All three backend packages importable."""
    monkeypatch.setattr(bb, "is_available", lambda name=None: True)


class TestResolveBackend:
    def test_explicit_camoufox_config_wins(self, monkeypatch):
        monkeypatch.setattr(bb, "is_available", lambda name=None: name == "camoufox")
        assert bb.resolve_backend({"browser_backend": "camoufox"}) == "camoufox"

    def test_unknown_value_resets_to_default(self, _all_backends):
        assert bb.resolve_backend({"browser_backend": "nonsense"}) == bb.DEFAULT_BACKEND

    def test_camoufox_missing_falls_to_cloakbrowser(self, monkeypatch):
        monkeypatch.setattr(
            bb, "is_available", lambda name=None: name != "camoufox")
        assert bb.resolve_backend({"browser_backend": "camoufox"}) == "cloakbrowser"

    def test_stale_cloakbrowser_redirects_to_camoufox(self, monkeypatch):
        monkeypatch.setattr(bb, "is_available", lambda name=None: True)
        monkeypatch.setattr(bb, "_cloakbrowser_dist_version", lambda: (0, 4, 0))
        monkeypatch.delenv("SCANSCI_ALLOW_OLD_CLOAKBROWSER", raising=False)
        assert bb.resolve_backend({"browser_backend": "cloakbrowser"}) == "camoufox"


def test_camoufox_launch_drops_chromium_args(monkeypatch):
    """Chromium flags must never reach the Firefox binary (they become
    open-URL arguments there); launch proceeds without them."""
    pytest.importorskip("playwright")
    pytest.importorskip("camoufox")
    captured: dict = {}

    fake_pw = type("PW", (), {})()

    class _FakeBrowser:
        version = "152.0"

        def new_context(self):
            return None

        def close(self):
            pass

    def fake_new_browser(pw, **kwargs):
        captured["args"] = kwargs.get("args")
        captured["proxy"] = kwargs.get("proxy")
        return _FakeBrowser()

    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: type("SP", (), {"start": staticmethod(lambda: fake_pw)})())
    monkeypatch.setattr(
        "camoufox.NewBrowser", lambda pw, **kw: fake_new_browser(pw, **kw))
    monkeypatch.setattr(
        "camoufox.DefaultAddons",
        type("DA", (), {"UBO": "ubo"}))

    bb._launch_camoufox(
        headless=True, proxy=None,
        args=["--disable-blink-features=AutomationControlled", "--window-size=1,1"],
        humanize=False)

    assert captured["args"] == []  # chromium flags dropped, not forwarded


if __name__ == "__main__":
    pytest.main([__file__])
