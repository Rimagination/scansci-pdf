"""ego-lite CDP attach tests (mocked; no real ego lite required)."""

from __future__ import annotations

import pytest

from scansci_pdf import browser_backend


class _FakePage:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self):
        self.closed = False
        self.pages = []

    def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.url = ""

    def connect_over_cdp(self, url):
        self.url = url
        return self.browser


class _FakePW:
    def __init__(self, browser):
        self.browser = browser
        self.chromium = _FakeChromium(browser)
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


class _FakeBrowser:
    def __init__(self, contexts=None):
        self.contexts = list(contexts or [])
        self.closed = False
        self.new_context_calls = 0

    def new_context(self, *args, **kwargs):
        self.new_context_calls += 1
        context = _FakeContext()
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def _config(**kwargs) -> dict:
    cfg = {
        "browser_backend": "ego-lite",
        "browser_headless": False,
        "browser_executable": "",
        "ego_lite_cdp_url": "http://127.0.0.1:9351",
        "ego_lite_executable": "",
        "ego_lite_auto_launch": False,
        "ego_lite_user_data_dir": "",
        "ego_lite_launch_timeout": 5.0,
    }
    cfg.update(kwargs)
    return cfg


def _patch_start(monkeypatch, browser):
    pw = _FakePW(browser)
    monkeypatch.setattr(
        browser_backend,
        "_ego_lite_start",
        lambda *args, **kwargs: (pw, browser, None, "http://127.0.0.1:9351"),
    )
    return pw


def test_launch_reuses_existing_context_and_does_not_close_browser(monkeypatch):
    existing = _FakeContext()
    browser = _FakeBrowser([existing])
    pw = _patch_start(monkeypatch, browser)

    launched = browser_backend.launch(config=_config(), headless=False)

    context = launched.new_context()
    assert context is existing
    context.close()
    assert existing.closed is False  # never close the user's logged-in context
    launched.close()
    assert pw.stopped is True
    assert browser.closed is False  # never send Browser.close over CDP


def test_persistent_context_reuses_user_context_without_closing_it(monkeypatch):
    existing = _FakeContext()
    browser = _FakeBrowser([existing])
    pw = _patch_start(monkeypatch, browser)

    context = browser_backend.launch_persistent_context(
        "ignored-profile", config=_config(), headless=False
    )

    assert context is existing
    page = context.new_page()
    context.close()
    assert pw.stopped is True
    assert existing.closed is False
    assert page.closed is False


def test_persistent_context_closes_only_a_context_scansci_created(monkeypatch):
    browser = _FakeBrowser([])
    pw = _patch_start(monkeypatch, browser)

    context = browser_backend.launch_persistent_context(
        "ignored-profile", config=_config(), headless=False
    )

    assert context is browser.contexts[0]
    context.close()
    assert context.closed is True
    assert pw.stopped is True


def test_missing_ego_lite_gives_actionable_error(monkeypatch):
    cfg = _config(ego_lite_cdp_url="")
    monkeypatch.setattr(browser_backend.platform, "system", lambda: "Windows")
    monkeypatch.setattr(browser_backend, "_ego_lite_executable", lambda config=None: "")
    monkeypatch.setattr(browser_backend, "_probe_cdp", lambda *args, **kwargs: False)

    with pytest.raises(RuntimeError, match="remote-debugging-port"):
        browser_backend.launch(config=cfg)


def test_ego_lite_is_unavailable_on_linux(monkeypatch):
    monkeypatch.setattr(browser_backend.platform, "system", lambda: "Linux")
    assert browser_backend.is_available("ego-lite", _config()) is False


def test_auto_launch_starts_ego_lite_and_connects(monkeypatch):
    cfg = _config(
        ego_lite_cdp_url="",
        ego_lite_executable="ego-browser",
        ego_lite_auto_launch=True,
        ego_lite_user_data_dir="/tmp/ego-profile",
    )
    monkeypatch.setattr(browser_backend.platform, "system", lambda: "Windows")
    monkeypatch.setattr(browser_backend, "_ego_lite_executable", lambda config=None: "ego-browser")

    probe_calls = {"n": 0}

    def probe(url, timeout=1.5):
        probe_calls["n"] += 1
        return probe_calls["n"] > 2

    monkeypatch.setattr(browser_backend, "_probe_cdp", probe)

    process = _FakeProcess()
    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(browser_backend.subprocess, "Popen", fake_popen)

    browser = _FakeBrowser([])
    pw = _FakePW(browser)
    import patchright.sync_api as patchright_api

    monkeypatch.setattr(patchright_api, "sync_playwright", lambda: pw)

    driver, connected, launched, url = browser_backend._ego_lite_start(
        cfg, headless=False, launch_if_needed=True
    )

    assert driver is pw
    assert connected is browser
    assert launched is process
    assert url == "http://127.0.0.1:9351"
    assert commands and "--remote-debugging-port=9351" in commands[0]
    assert "--user-data-dir=/tmp/ego-profile" in commands[0]

    browser_backend._release_ego_lite(driver, launched)
    assert pw.stopped is True
    assert process.terminated is True
