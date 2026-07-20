from __future__ import annotations

import json
import os
import stat

from scansci_pdf import browser_login


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url

    def goto(self, *_args, **_kwargs) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.login_page = _FakePage("https://lib.example.edu/login")
        self.success_page = _FakePage("https://publisher-com.proxy.example.edu/")
        self.pages = [self.login_page, self.success_page]
        self.closed = False

    def new_page(self) -> _FakePage:
        return self.login_page

    def cookies(self):
        return [
            {
                "name": "session",
                "value": "test-value",
                "domain": ".proxy.example.edu",
                "path": "/",
            }
        ]

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.closed = False

    def new_context(self) -> _FakeContext:
        return self.context

    def close(self) -> None:
        self.closed = True


def test_open_login_browser_detects_success_in_new_tab(monkeypatch, tmp_path):
    context = _FakeContext()
    browser = _FakeBrowser(context)
    cookie_file = tmp_path / "ezproxy_cookies.json"

    monkeypatch.setattr(browser_login, "_HAS_CLOAKBROWSER", True)
    monkeypatch.setattr(browser_login, "launch", lambda **_kwargs: browser)
    monkeypatch.setattr(browser_login.time, "sleep", lambda _seconds: None)

    def detect_login(_context, page) -> bool:
        return page.url.endswith(".proxy.example.edu/")

    result = browser_login.open_login_browser(
        "https://lib.example.edu/login",
        {},
        cookie_file=cookie_file,
        detect_login=detect_login,
        max_wait=3,
        auto_import=False,
    )

    assert result is True
    assert json.loads(cookie_file.read_text(encoding="utf-8"))[0]["name"] == "session"
    assert browser.closed is True


def test_open_login_browser_allows_manual_confirmation(monkeypatch, tmp_path):
    context = _FakeContext()
    browser = _FakeBrowser(context)
    cookie_file = tmp_path / "ezproxy_cookies.json"

    monkeypatch.setattr(browser_login, "_HAS_CLOAKBROWSER", True)
    monkeypatch.setattr(browser_login, "launch", lambda **_kwargs: browser)
    monkeypatch.setattr("builtins.input", lambda: "")

    result = browser_login.open_login_browser(
        "https://lib.example.edu/login",
        {},
        cookie_file=cookie_file,
        manual_confirm=True,
        auto_import=False,
    )

    assert result is True
    assert json.loads(cookie_file.read_text(encoding="utf-8"))[0]["name"] == "session"
    if os.name != "nt":
        assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(cookie_file.with_suffix(".txt").stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))
    assert browser.closed is True
