"""Tests for institutional session self-healing (auto_relogin)."""

import requests

from scansci_pdf.session_heal import ensure_institutional_sessions, ensure_webvpn_session


class FakeResponse:
    def __init__(self, url, status_code=200):
        self.url = url
        self.status_code = status_code


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.cookies = requests.cookies.RequestsCookieJar()
        self.trust_env = True
        self.headers = {}

    def get(self, url, **kwargs):
        return self._response


class FakeRequestsModule:
    """Stands in for the requests module; Session must not be an instance method."""

    @staticmethod
    def Session():
        return FakeSession(FakeResponse("https://vpn.example.org/https-www.nature.com"))


class FakeRequestsModuleLoginRedirect(FakeRequestsModule):
    @staticmethod
    def Session():
        return FakeSession(FakeResponse("https://cas.example.org/login", 200))


def _jar_with_cookie():
    jar = requests.cookies.RequestsCookieJar()
    jar.set("wrdvpn", "abc", domain="vpn.example.org", path="/")
    return jar


def test_session_status_none_without_cookies(monkeypatch):
    from scansci_pdf.sources import instsci

    monkeypatch.setattr(instsci, "_load_cookies", lambda config: requests.cookies.RequestsCookieJar())
    assert instsci.session_status({"cache_dir": "x"}) == "none"


def test_session_status_none_without_base(monkeypatch):
    from scansci_pdf.sources import instsci

    monkeypatch.setattr(instsci, "_load_cookies", lambda config: _jar_with_cookie())
    assert instsci.session_status({"cache_dir": "x"}) == "none"


def test_session_status_valid(monkeypatch):
    from scansci_pdf.sources import instsci

    monkeypatch.setattr(instsci, "_load_cookies", lambda config: _jar_with_cookie())
    monkeypatch.setattr(instsci, "_get_webvpn_base", lambda config: "https://vpn.example.org")
    monkeypatch.setattr(instsci, "requests", FakeRequestsModule)
    assert instsci.session_status({"cache_dir": "x"}) == "valid"


def test_session_status_expired_on_login_redirect(monkeypatch):
    from scansci_pdf.sources import instsci

    monkeypatch.setattr(instsci, "_load_cookies", lambda config: _jar_with_cookie())
    monkeypatch.setattr(instsci, "_get_webvpn_base", lambda config: "https://vpn.example.org")
    monkeypatch.setattr(instsci, "requests", FakeRequestsModuleLoginRedirect)
    assert instsci.session_status({"cache_dir": "x"}) == "expired"


def test_session_status_unreachable_on_exception(monkeypatch):
    from scansci_pdf.sources import instsci

    monkeypatch.setattr(instsci, "_load_cookies", lambda config: _jar_with_cookie())
    monkeypatch.setattr(instsci, "_get_webvpn_base", lambda config: "https://vpn.example.org")

    class BrokenSession:
        cookies = requests.cookies.RequestsCookieJar()
        trust_env = True
        headers = {}

        def get(self, url, **kwargs):
            raise requests.ConnectionError("down")

    monkeypatch.setattr(instsci, "requests", type("R", (), {"Session": BrokenSession})())
    assert instsci.session_status({"cache_dir": "x"}) == "unreachable"


def test_auto_relogin_disabled_never_logs_in(monkeypatch):
    from scansci_pdf.sources import instsci

    calls = []
    monkeypatch.setattr(instsci, "session_status", lambda config: "expired")
    monkeypatch.setattr(instsci, "instsci_login", lambda config: calls.append(1) or True)

    report = ensure_webvpn_session({"auto_relogin": False})

    assert report["status"] == "disabled"
    assert calls == []


def test_valid_session_does_not_relogin(monkeypatch):
    from scansci_pdf.sources import instsci

    monkeypatch.setattr(instsci, "session_status", lambda config: "valid")
    monkeypatch.setattr(instsci, "instsci_login", lambda config: (_ for _ in ()).throw(AssertionError("login must not run")))

    report = ensure_webvpn_session({"auto_relogin": True})

    assert report["status"] == "valid"


def test_none_and_unreachable_sessions_do_not_relogin(monkeypatch):
    from scansci_pdf.sources import instsci

    for status in ("none", "unreachable"):
        monkeypatch.setattr(instsci, "session_status", lambda config, s=status: s)
        monkeypatch.setattr(instsci, "instsci_login", lambda config: (_ for _ in ()).throw(AssertionError("login must not run")))
        assert ensure_webvpn_session({"auto_relogin": True})["status"] == status


def test_expired_session_relogs_in_and_confirms(monkeypatch):
    from scansci_pdf.sources import instsci

    state = {"calls": 0}

    def fake_status(config):
        return "valid" if state["calls"] else "expired"

    monkeypatch.setattr(instsci, "session_status", fake_status)
    monkeypatch.setattr(instsci, "instsci_login", lambda config: state.__setitem__("calls", state["calls"] + 1) or True)

    report = ensure_webvpn_session({"auto_relogin": True})

    assert state["calls"] == 1
    assert report["status"] == "refreshed"


def test_expired_session_with_failed_login_is_reported(monkeypatch):
    from scansci_pdf.sources import instsci

    monkeypatch.setattr(instsci, "session_status", lambda config: "expired")
    monkeypatch.setattr(instsci, "instsci_login", lambda config: False)

    report = ensure_webvpn_session({"auto_relogin": True})

    assert report["status"] == "login_failed"


def test_institutional_sessions_skipped_when_not_configured():
    assert ensure_institutional_sessions({"auto_relogin": True, "vpnsci_enabled": False}) == {}
    assert ensure_institutional_sessions({"auto_relogin": True, "vpnsci_enabled": True}) == {}


def test_institutional_sessions_reports_webvpn_when_configured(monkeypatch):
    from scansci_pdf.sources import instsci

    monkeypatch.setattr(instsci, "session_status", lambda config: "valid")
    monkeypatch.setattr(instsci, "instsci_login", lambda config: True)

    report = ensure_institutional_sessions(
        {"auto_relogin": True, "vpnsci_enabled": True, "vpnsci_base_url": "https://vpn.example.org", "vpnsci_school": ""}
    )

    assert report["webvpn"]["status"] == "valid"
