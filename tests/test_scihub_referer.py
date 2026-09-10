"""Sci-Hub PDF CDN requires a same-site Referer (sci.bban.top 403s without)."""

from __future__ import annotations

from pathlib import Path

import scansci_pdf.sources.scihub as scihub


class _Resp:
    status_code = 200
    headers = {"content-type": "application/pdf"}

    def __init__(self):
        self._iter = iter([b"%PDF-1.4 " + b"x" * 12_000])

    def iter_content(self, chunk_size=8192):
        return self._iter


class _Capture:
    """Captures the headers the session would send."""

    headers: dict = {}

    def get(self, url, **kwargs):
        _Capture.headers = dict(self.headers)
        return _Resp()


def test_referer_threaded_to_download(monkeypatch, tmp_path: Path):
    """try_scihub_domain passes the mirror landing URL as Referer."""
    captured: dict = {}

    class _Page:
        status_code = 200
        headers = {"content-type": "text/html"}
        cookies = {"session": "x"}
        url = "https://sci-hub.vg/10.1/x"
        _content = b""  # no buffered body; the flow falls back to resp.raw

        def __init__(self):
            self._iter = iter([
                b'<html><meta name="citation_pdf_url" '
                b'content="https://sci.bban.top/pdf/10.1/x.pdf"></html>'])

        def iter_content(self, chunk_size=8192):
            return self._iter

    class _Session:
        headers: dict = {}
        cookies: dict = {}

        def get(self, url, **kwargs):
            captured["headers"] = dict(self.headers)
            return _Page()

    monkeypatch.setattr(scihub, "fetch", lambda *a, **k: _Page())
    monkeypatch.setattr(
        "scansci_pdf.pdf_utils.download_pdf",
        lambda url, out, cfg, src, **kw: captured.update(kw) or None)

    scihub.try_scihub_domain(
        "10.1/x", "https://sci-hub.vg", tmp_path / "o.pdf",
        {"scihub_browser_first": False})

    assert captured.get("referer") == "https://sci-hub.vg/10.1/x"


def test_download_pdf_sets_referer_header(monkeypatch, tmp_path: Path):
    """download_pdf sends the Referer header on the cookie session path."""
    seen: dict = {}

    import scansci_pdf.pdf_utils as pu

    class _S:
        headers: dict = {}
        cookies: dict = {}

        def get(self, url, **kwargs):
            seen["headers"] = dict(self.headers)
            return _Resp()

    monkeypatch.setattr("requests.Session", lambda: _S())

    pu.download_pdf("https://sci.bban.top/pdf/10.1/x.pdf", tmp_path / "o.pdf",
                    {}, "test", require_pdf_like_url=False,
                    cookies={"a": "b"}, referer="https://sci-hub.vg/10.1/x")

    assert seen["headers"].get("Referer") == "https://sci-hub.vg/10.1/x"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__])
