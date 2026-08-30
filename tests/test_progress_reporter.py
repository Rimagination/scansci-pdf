from __future__ import annotations

from scansci_pdf import progress_reporter


def test_manual_attention_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANSCI_PDF_DATA_DIR", str(tmp_path))

    progress_reporter.start_task("文献下载", total=3)
    assert progress_reporter.read_state()["attention"] == {}

    progress_reporter.set_attention(
        "publisher:a",
        "请在浏览器窗口完成安全验证",
        current="10.1000/a",
        phase="人工验证",
    )
    progress_reporter.set_attention(
        "publisher:b",
        "请在浏览器窗口完成安全验证",
        current="10.1000/b",
        phase="人工验证",
    )
    state = progress_reporter.read_state()
    assert set(state["attention"]) == {"publisher:a", "publisher:b"}
    assert state["attention"]["publisher:b"]["current"] == "10.1000/b"

    progress_reporter.advance(True, current="10.1000/c")
    assert progress_reporter.read_state()["done"] == 1

    progress_reporter.clear_attention("publisher:a")
    assert set(progress_reporter.read_state()["attention"]) == {"publisher:b"}
    progress_reporter.finish()
    state = progress_reporter.read_state()
    assert state["status"] == "done"
    assert state["attention"] == {}


def test_challenge_wait_publishes_and_clears_attention(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANSCI_PDF_DATA_DIR", str(tmp_path))
    progress_reporter.start_task("文献下载", total=1)

    from scansci_pdf import fetcher

    class FakePage:
        def __init__(self):
            self.calls = 0
            self.attention_seen = False

        def title(self):
            self.calls += 1
            if self.calls == 1:
                return "请稍候..."
            self.attention_seen = bool(progress_reporter.read_state()["attention"])
            return "article"

    page = FakePage()
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)
    fetcher._wait_for_challenge(page, max_tries=2, current="10.1000/a")

    assert page.attention_seen
    assert progress_reporter.read_state()["attention"] == {}


def test_publisher_challenge_wait_uses_the_same_attention_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANSCI_PDF_DATA_DIR", str(tmp_path))
    progress_reporter.start_task("出版商下载", total=1)

    from scansci_pdf.publisher_batch import DownloadResult, PublisherBatchDownloader

    downloader = PublisherBatchDownloader.__new__(PublisherBatchDownloader)
    events = []
    downloader.login_timeout_sec = 0
    downloader._event = lambda result, kind, detail: events.append(kind)

    class FakePage:
        url = "https://example.test/article"

        def __init__(self):
            self.calls = 0
            self.attention_seen = False

    page = FakePage()

    def is_challenge(_page):
        page.calls += 1
        page.attention_seen |= bool(progress_reporter.read_state()["attention"])
        return page.calls == 1

    downloader._is_challenge_page = is_challenge
    monkeypatch.setattr("scansci_pdf.publisher_batch.time.sleep", lambda _seconds: None)
    result = DownloadResult(doi="10.1000/a", status="failed")
    assert downloader._wait_for_challenge(page, result)

    assert page.attention_seen
    assert "challenge_manual_wait" in events
    assert "challenge_resolved" in events
    assert progress_reporter.read_state()["attention"] == {}
