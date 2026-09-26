"""Regression: _check_browser_backend() must not crash on the patchright path.

`_HAS_BROWSER_BACKEND` is a module-level cache, but the function assigning it
never declared it `global` — so the name was local for the whole function and
the `if _HAS_BROWSER_BACKEND is None:` read raised UnboundLocalError.
patchright is the default backend since 1.17, so the crash hit every browser
download path unless the backend was explicitly switched to CloakBrowser or
Camoufox (both of which return before the assignment).
"""

from __future__ import annotations

import pytest

import scansci_pdf.browser_backend as bb
from scansci_pdf import browser_engine


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch):
    """Every test starts from an unset sentinel, and leaves it unset."""
    monkeypatch.setattr(browser_engine, "_HAS_BROWSER_BACKEND", None, raising=False)


class TestCheckBrowserBackend:
    def test_patchright_default_returns_true(self, monkeypatch):
        monkeypatch.setattr(bb, "resolve_backend", lambda config=None: bb.BACKEND_PATCHRIGHT)
        monkeypatch.setattr(bb, "is_available", lambda name=None: True)
        assert browser_engine._check_browser_backend() is True

    def test_patchright_probe_is_cached(self, monkeypatch):
        probed: list = []
        monkeypatch.setattr(bb, "resolve_backend", lambda config=None: bb.BACKEND_PATCHRIGHT)
        monkeypatch.setattr(
            bb, "is_available", lambda name=None: (probed.append(name), True)[1]
        )
        assert browser_engine._check_browser_backend() is True
        assert browser_engine._check_browser_backend() is True
        assert probed == [bb.BACKEND_PATCHRIGHT]  # second call served by the cache

    def test_missing_patchright_returns_false(self, monkeypatch):
        monkeypatch.setattr(bb, "resolve_backend", lambda config=None: bb.BACKEND_PATCHRIGHT)
        monkeypatch.setattr(bb, "is_available", lambda name=None: False)
        assert browser_engine._check_browser_backend() is False

    def test_cloakbrowser_branch_is_untouched(self, monkeypatch):
        """The early-return branches must not touch the patchright cache."""
        monkeypatch.setattr(bb, "resolve_backend", lambda config=None: bb.BACKEND_CLOAKBROWSER)
        monkeypatch.setattr(bb, "is_available", lambda name=None: True)
        assert browser_engine._check_browser_backend() is True
        assert browser_engine._HAS_BROWSER_BACKEND is None


if __name__ == "__main__":
    pytest.main([__file__])
