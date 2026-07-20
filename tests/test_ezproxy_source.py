from __future__ import annotations

import sys
import types

from scansci_pdf.sources import ezproxy


class _FakeResponse:
    headers = {"content-type": "application/pdf"}
    url = "https://pdf.example.edu/article.pdf"

    @staticmethod
    def body() -> bytes:
        return b"%PDF-" + (b"x" * 6000)


class _FakePage:
    def __init__(self) -> None:
        self.url = ""
        self._response_handler = None
        self.pdf_waits = 0

    def on(self, event, handler) -> None:
        if event == "response":
            self._response_handler = handler

    def goto(self, url, **_kwargs) -> None:
        if "/login?" in url:
            self.url = "https://publisher-com.proxy.example.edu/article"
        else:
            self.url = url

    def evaluate(self, _script):
        if self.url.endswith("/article"):
            return "https://publisher-com.proxy.example.edu/article.pdf"
        return ""

    def content(self) -> str:
        if self.url.endswith(".pdf") and self.pdf_waits < 2:
            return "<html><title>Processing Verification</title></html>"
        return "<html></html>"

    def title(self) -> str:
        if self.url.endswith(".pdf") and self.pdf_waits < 2:
            return "Processing Verification"
        return "PDF"

    def tick(self) -> None:
        if not self.url.endswith(".pdf"):
            return
        self.pdf_waits += 1
        if self.pdf_waits == 2 and self._response_handler:
            self._response_handler(_FakeResponse())


class _FakeArticleChallengePage(_FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.article_waits = 0

    def evaluate(self, script):
        if self.url.endswith("/article") and self.article_waits < 2:
            return ""
        return super().evaluate(script)

    def content(self) -> str:
        if self.url.endswith("/article") and self.article_waits < 2:
            return "<html><title>Processing Verification</title></html>"
        return super().content()

    def title(self) -> str:
        if self.url.endswith("/article") and self.article_waits < 2:
            return "Processing Verification"
        return super().title()

    def tick(self) -> None:
        if self.url.endswith("/article"):
            self.article_waits += 1
            return
        super().tick()


class _FakeElsevierMetadataPage(_FakePage):
    def goto(self, url, **_kwargs) -> None:
        if "/login?" in url:
            self.url = (
                "https://www-sciencedirect-com.proxy.example.edu/"
                "science/article/pii/S1359645426006208/article"
            )
        else:
            self.url = url

    def evaluate(self, _script):
        return ""

    def content(self) -> str:
        if self.url.endswith("/article"):
            return (
                '<script>"pdfDownload":{"isPdfFullText":true,'
                '"urlMetadata":{"queryParams":{"md5":"abc123",'
                '"pid":"pid456"},"pii":"S1359645426006208",'
                '"pdfExtension":".pdf","path":"science/article/pii"}}</script>'
            )
        return super().content()

    def tick(self) -> None:
        if ".pdf?" in self.url and self._response_handler:
            self._response_handler(_FakeResponse())
            return
        super().tick()


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    def new_page(self) -> _FakePage:
        return self.page

    def add_cookies(self, _cookies) -> None:
        return None


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.context = _FakeContext(page)
        self.closed = False

    def new_context(self) -> _FakeContext:
        return self.context

    def close(self) -> None:
        self.closed = True


def test_ezproxy_waits_for_pdf_verification_to_finish(monkeypatch, tmp_path):
    page = _FakePage()
    browser = _FakeBrowser(page)
    fake_cloakbrowser = types.SimpleNamespace(launch=lambda **_kwargs: browser)
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake_cloakbrowser)
    monkeypatch.setattr(
        ezproxy.requests,
        "head",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            url="https://publisher.com/article"
        ),
    )
    monkeypatch.setattr(ezproxy.time, "sleep", lambda _seconds: page.tick())

    output_path = tmp_path / "paper.pdf"
    result = ezproxy.try_ezproxy(
        "10.1234/example",
        output_path,
        {
            "ezproxy_enabled": True,
            "ezproxy_login_url": "https://proxy.example.edu/login?url={url}",
            "cache_dir": str(tmp_path / "cache"),
            "ezproxy_challenge_timeout": 30,
        },
    )

    assert result is not None
    assert result["success"] is True
    assert output_path.read_bytes().startswith(b"%PDF-")
    assert browser.closed is True


def test_ezproxy_waits_for_article_verification_before_finding_pdf(monkeypatch, tmp_path):
    page = _FakeArticleChallengePage()
    browser = _FakeBrowser(page)
    fake_cloakbrowser = types.SimpleNamespace(launch=lambda **_kwargs: browser)
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake_cloakbrowser)
    monkeypatch.setattr(
        ezproxy.requests,
        "head",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            url="https://publisher.com/article"
        ),
    )
    monkeypatch.setattr(ezproxy.time, "sleep", lambda _seconds: page.tick())

    output_path = tmp_path / "paper.pdf"
    result = ezproxy.try_ezproxy(
        "10.1234/example",
        output_path,
        {
            "ezproxy_enabled": True,
            "ezproxy_login_url": "https://proxy.example.edu/login?url={url}",
            "cache_dir": str(tmp_path / "cache"),
            "ezproxy_challenge_timeout": 30,
        },
    )

    assert result is not None
    assert result["success"] is True
    assert output_path.read_bytes().startswith(b"%PDF-")
    assert browser.closed is True


def test_ezproxy_uses_elsevier_metadata_and_preserves_proxy_host(monkeypatch, tmp_path):
    page = _FakeElsevierMetadataPage()
    browser = _FakeBrowser(page)
    fake_cloakbrowser = types.SimpleNamespace(launch=lambda **_kwargs: browser)
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake_cloakbrowser)
    monkeypatch.setattr(
        ezproxy.requests,
        "head",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            url="https://www.sciencedirect.com/science/article/pii/S1359645426006208"
        ),
    )
    monkeypatch.setattr(ezproxy.time, "sleep", lambda _seconds: page.tick())

    output_path = tmp_path / "paper.pdf"
    result = ezproxy.try_ezproxy(
        "10.1016/example",
        output_path,
        {
            "ezproxy_enabled": True,
            "ezproxy_login_url": "https://proxy.example.edu/login?url={url}",
            "cache_dir": str(tmp_path / "cache"),
            "ezproxy_challenge_timeout": 30,
        },
    )

    assert result is not None
    assert result["success"] is True
    assert page.url == (
        "https://www-sciencedirect-com.proxy.example.edu/"
        "science/article/pii/S1359645426006208.pdf?md5=abc123&pid=pid456"
    )
