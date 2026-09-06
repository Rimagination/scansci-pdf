"""Regression tests: WebVPN auth must fail fast and never leak a Playwright loop."""

import pytest

from scansci_pdf.auth import WebVPNAuth


CFG = {"instsci_school": "", "vpnsci_school": "", "instsci_base_url": "",
       "vpnsci_base_url": ""}


class TestConvertUrlEmptyBase:
    def test_convert_url_raises_on_empty_base(self):
        """Used to return '/https/77726476...' — a relative URL that callers
        then navigated to as 'Cannot navigate to invalid URL'."""
        auth = WebVPNAuth(dict(CFG))
        with pytest.raises(ValueError):
            auth.convert_url("https://example.com/article/pii/X")

    def test_validate_session_false_when_no_base(self):
        auth = WebVPNAuth(dict(CFG))
        assert auth._validate_session() is False

    def test_convert_url_ok_with_base(self):
        auth = WebVPNAuth(dict(CFG), base_url="https://webvpn.example.edu.cn")
        url = auth.convert_url("https://www.sciencedirect.com/x")
        assert url.startswith("https://webvpn.example.edu.cn/https/")
        assert "77726476706e69737468656265737421" in url  # default IV hex prefix


class TestBrowserLoginFailFast:
    def test_no_browser_launched_when_base_empty(self, monkeypatch, tmp_path):
        """The leak: _browser_login used to launch the persistent context
        FIRST and only then discover the empty gateway, returning without
        closing it — poisoning the thread's Playwright loop."""
        import scansci_pdf.auth as auth_mod

        launched = []

        def _fake_launch(**kwargs):
            launched.append(kwargs)
            raise AssertionError("browser must not be launched when webvpn base is empty")

        monkeypatch.setattr(auth_mod, "_HAS_CLOAKBROWSER", True)
        monkeypatch.setattr("scansci_pdf.browser_backend.launch_persistent_context", _fake_launch)
        monkeypatch.setattr(auth_mod, "_get_profile_dir", lambda cfg: tmp_path)

        auth = WebVPNAuth(dict(CFG))
        assert auth._browser_login() is False
        assert launched == []
        assert auth._context is None
