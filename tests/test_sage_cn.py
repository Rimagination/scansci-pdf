"""Offline tests for the authorized SAGE China route."""

from __future__ import annotations

import json
from pathlib import Path

import requests

from scansci_pdf import _publisher_strategies_core as core
from scansci_pdf.publisher_strategies import StrategyRegistry
from scansci_pdf.sources.carsi import detect_publisher
from scansci_pdf.sources.sage_cn import (
    extract_article_id,
    has_saved_sage_cn_session,
    try_sage_cn_authorized,
)


DOI = "10.1177/00472875261441572"
ARTICLE_ID = "E9B3AA9AE4384B20910A5AE61E17A89F"
RELATED_ID = "536880EE98A242A7934CC56B3E680C42"


def _save_cookie(cache_dir: Path, *, expires: float = 0) -> None:
    cookie_dir = cache_dir / "carsi_cookies"
    cookie_dir.mkdir(parents=True, exist_ok=True)
    (cookie_dir / "sage.json").write_text(
        json.dumps([
            {
                "name": "token",
                "value": "saved-session",
                "domain": "sage.cnpereading.com",
                "path": "/",
                "expires": expires,
            }
        ]),
        encoding="utf-8",
    )


def test_extract_article_id_matches_current_doi_not_related_article():
    html = rf"""
    <script>
      {{\"articleId\":\"{RELATED_ID}\",\"doi\":\"10.1177/1094670520904417\"}},
      {{\"articleId\":\"{ARTICLE_ID}\",\"doi\":\"{DOI}\"}}
    </script>
    """
    assert extract_article_id(html, DOI) == ARTICLE_ID
    assert extract_article_id(html, "10.1177/not-present") == ""


def test_carsi_detects_cn_frontend_before_global_sage():
    assert detect_publisher(f"https://sage.cnpereading.com/doi/{DOI}") == "sage-cn"


def test_sage_strategy_does_not_guess_cn_pdf_url():
    strategy = StrategyRegistry.get_by_name("SAGE")
    assert strategy is not None
    assert all("sage.cnpereading.com/doi/pdf/" not in url for url in strategy.pdf_urls(DOI))


def test_saved_sage_cn_session_requires_live_domain_cookie(tmp_path):
    config = {"cache_dir": str(tmp_path)}
    assert not has_saved_sage_cn_session(config)

    _save_cookie(tmp_path, expires=1)
    assert not has_saved_sage_cn_session(config)

    _save_cookie(tmp_path)
    assert has_saved_sage_cn_session(config)


class _FakeResponse:
    def __init__(self, *, text: str = "", body: bytes = b"", content_type: str = "text/html"):
        self.status_code = 200
        self.text = text
        self._body = body
        self.headers = {"content-type": content_type}

    def iter_content(self, chunk_size: int = 8192):
        del chunk_size
        yield self._body


class _FakeSession:
    def __init__(self, article_html: str, pdf_body: bytes):
        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.article_html = article_html
        self.pdf_body = pdf_body
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if "journal/download" in url:
            return _FakeResponse(body=self.pdf_body, content_type="application/pdf")
        return _FakeResponse(text=self.article_html)


def test_authorized_download_uses_real_article_id_endpoint(tmp_path):
    config = {"cache_dir": str(tmp_path), "publisher_timeout": 9}
    _save_cookie(tmp_path)
    article_html = (
        f'{{"articleId":"{RELATED_ID}","doi":"10.1177/related"}},'
        f'{{"articleId":"{ARTICLE_ID}","doi":"{DOI}"}}'
    )
    pdf_body = b"%PDF-1.7\n" + (b"x" * 1500) + b"\n%%EOF"
    session = _FakeSession(article_html, pdf_body)
    output_path = tmp_path / "paper.pdf"

    assert try_sage_cn_authorized(DOI, output_path, config, session=session)
    assert output_path.read_bytes() == pdf_body
    assert len(session.calls) == 2
    download_url, download_kwargs = session.calls[1]
    assert download_url.endswith("/website/journal/download")
    assert download_kwargs["params"] == {"articleId": ARTICLE_ID}
    assert download_kwargs["headers"]["Referer"].endswith(f"/doi/{DOI}")


def test_authorized_download_does_not_make_request_without_saved_session(tmp_path):
    config = {"cache_dir": str(tmp_path)}
    session = _FakeSession("", b"")
    assert not try_sage_cn_authorized(DOI, tmp_path / "paper.pdf", config, session=session)
    assert session.calls == []


def test_try_sage_browser_prefers_authorized_cn_route(monkeypatch, tmp_path):
    output_path = tmp_path / "paper.pdf"
    output_path.write_bytes(b"%PDF-1.7\n" + (b"x" * 1500) + b"\n%%EOF")
    monkeypatch.setattr(
        "scansci_pdf.sources.sage_cn.try_sage_cn_authorized",
        lambda doi, path, config: True,
    )

    result = core.try_sage_browser(DOI, output_path, {})
    assert result is not None
    assert result["success"] is True
    assert result["source"] == "SAGE-CN(CARSI)"


def test_visible_browser_forwards_selected_backend_config(monkeypatch, tmp_path):
    from scansci_pdf import browser_engine

    config = {
        "cache_dir": str(tmp_path),
        "browser_backend": "cloakbrowser",
        "browser_executable": "/example/Chromium",
    }
    seen: dict = {}

    class FakeContext:
        closed = False

        def new_page(self):
            return object()

        def add_cookies(self, cookies):
            del cookies

        def close(self):
            self.closed = True

    context = FakeContext()

    def fake_launch_persistent_context(user_data_dir, **kwargs):
        seen["user_data_dir"] = user_data_dir
        seen["kwargs"] = kwargs
        return context

    monkeypatch.setattr(browser_engine, "is_available", lambda cfg: True)
    monkeypatch.setattr(browser_engine, "close_shared_browser", lambda cfg: None)
    monkeypatch.setattr(core, "launch_persistent_context", fake_launch_persistent_context)

    with core._visible_browser(config, "sage"):
        pass

    assert seen["kwargs"]["config"] is config
    assert seen["kwargs"]["headless"] is False
    assert context.closed is True
