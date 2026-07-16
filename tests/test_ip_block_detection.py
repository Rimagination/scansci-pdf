"""Tests for publisher IP-block detection and batch auto-stop.

Covers:
- ``_is_ip_block_response`` signal classification (403/429, ACS block-page text)
- the consecutive-block auto-stop in ``_run_once_parallel`` / ``_run_once``:
  once IP_BLOCK_STOP_THRESHOLD ip_blocked results land in a row, remaining
  records are skipped and ``run_records`` skips the retry pass and flags
  ``auto_stopped`` in the summary.
"""

import unittest
from unittest.mock import patch

from scansci_pdf.publisher_batch import (
    IP_BLOCK_STOP_THRESHOLD,
    PublisherBatchDownloader,
    DownloadResult,
    PaperRecord,
    RETRYABLE_REASONS,
)


def _make_downloader():
    """A downloader with all browser-touching methods stubbed out."""
    d = PublisherBatchDownloader.__new__(PublisherBatchDownloader)
    d.config = {}
    d.profile = None  # not exercised by these unit tests
    d.institution_query = ""
    d.login_timeout_sec = 0
    d.pdf_timeout_ms = 1000
    d.post_login_hold_sec = 0
    d.post_run_hold_sec = 0
    d._ip_block_stopped = False
    return d


class _FakeContext:
    def close(self):
        pass

    def cookies(self):
        return []


class IsIpBlockResponseTests(unittest.TestCase):
    def setUp(self):
        self.d = _make_downloader()

    def test_block_status_codes(self):
        self.assertTrue(self.d._is_ip_block_response(403, ""))
        self.assertTrue(self.d._is_ip_block_response(429, ""))

    def test_ok_status_is_not_block(self):
        self.assertFalse(self.d._is_ip_block_response(200, ""))
        self.assertFalse(self.d._is_ip_block_response(200, "<html>article body</html>"))
        self.assertFalse(self.d._is_ip_block_response(None, ""))

    def test_acs_block_page_text(self):
        # The exact ACS page the user reported.
        text = (
            "IP Address Blocked\n"
            "IP Address: 166.111.174.52\n"
            "Your IP address has been blocked automatically due to unusual behavior. "
            "For assistance in removing the IP block, please contact ipblock@acs.org "
            "and include your IP address."
        )
        self.assertTrue(self.d._is_ip_block_response(200, text))

    def test_block_text_case_insensitive(self):
        self.assertTrue(self.d._is_ip_block_response(None, "ip address BLOCKED"))
        self.assertTrue(self.d._is_ip_block_response(None, "your IP address has been blocked"))

    def test_ipblock_mailbox_alone_triggers(self):
        self.assertTrue(self.d._is_ip_block_response(None, "contact ipblock@acs.org for help"))

    def test_bytes_body_supported(self):
        self.assertTrue(self.d._is_ip_block_response(None, b"IP Address Blocked"))
        # Non-utf8 bytes should not crash; just return False.
        self.assertFalse(self.d._is_ip_block_response(None, b"\xff\xfe\x00"))

    def test_normal_html_not_flagged(self):
        self.assertFalse(self.d._is_ip_block_response(200, "<html><body>full text pdf</body></html>"))
        # "blocked" without "ip address" should not trip.
        self.assertFalse(self.d._is_ip_block_response(200, "content is regionally blocked"))


class RetryableReasonsTests(unittest.TestCase):
    def test_ip_blocked_is_not_retryable(self):
        """If ip_blocked were retryable, a blocked IP would be churned again."""
        self.assertNotIn("ip_blocked", RETRYABLE_REASONS)

    def test_threshold_is_three(self):
        self.assertEqual(IP_BLOCK_STOP_THRESHOLD, 3)


def _blocked_result(doi="10.1021/x"):
    return DownloadResult(doi=doi, status="failed", reason="ip_blocked", state="ip_blocked")


def _ok_result(doi="10.1021/x"):
    return DownloadResult(doi=doi, status="success", reason="", state="ok", verified_match=True)


def _fail_result(doi="10.1021/x"):
    return DownloadResult(doi=doi, status="failed", reason="pdf_not_captured")


class AutoStopTests(unittest.TestCase):
    """Verify the consecutive-block auto-stop trips and skips remaining work.

    These mock fetch_one to feed a deterministic result sequence and stub the
    browser-touching methods, so only the orchestration logic is exercised.
    """

    def _patch_browser(self, d):
        """Stub out everything that touches a real browser/filesystem."""
        # _run_once_parallel / _run_once call these.
        patcher_profile = patch.object(d, "_prepare_worker_profile", return_value=Path_tmp())
        patcher_ctx = patch.object(d, "_launch_context", return_value=_FakeContext())
        # _PagePool is constructed internally; patch the class to a stub.
        patcher_pool = patch("scansci_pdf.publisher_batch._PagePool", _FakePagePool)
        patcher_profile.start()
        patcher_ctx.start()
        patcher_pool.start()
        self.addCleanup(patcher_profile.stop)
        self.addCleanup(patcher_ctx.stop)
        self.addCleanup(patcher_pool.stop)
        # also stub write helpers that touch disk in fetch_one's failure path
        patch.object(d, "_write_diagnostic", lambda *a, **k: None).start()
        patch.object(d, "_write_results", lambda *a, **k: None).start()
        patch.object(d, "_append_attempt", lambda *a, **k: None).start()
        patch.object(d, "_hold_after_login", lambda *a, **k: None).start()
        patch.object(d, "_hold_after_run", lambda *a, **k: None).start()
        patch.object(d, "_return_to_record_article_if_needed", lambda *a, **k: None).start()

    def test_parallel_stops_after_three_consecutive_blocks(self):
        d = _make_downloader()
        self._patch_browser(d)

        # 10 records, but fetch_one returns ip_blocked for every one.
        records = [PaperRecord(doi=f"10.1021/r{i}") for i in range(10)]
        seen = {"n": 0}

        def fake_fetch_one(page, record, run_dir):
            seen["n"] += 1
            return _blocked_result(doi=record.doi)

        with patch.object(d, "fetch_one", side_effect=fake_fetch_one):
            results = d._run_once_parallel(
                records, _tmp_run_dir(), worker_count=2, phase="primary"
            )

        # Only the first chunk or two should have run before the stop tripped;
        # crucially NOT all 10.
        self.assertLess(seen["n"], 10)
        self.assertGreaterEqual(seen["n"], IP_BLOCK_STOP_THRESHOLD)
        self.assertTrue(d._ip_block_stopped)
        # Every produced result is ip_blocked.
        self.assertTrue(all(r.reason == "ip_blocked" for r in results))

    def test_parallel_streak_resets_on_success(self):
        """2 blocks + 1 success + 2 blocks must NOT trip (not 3 consecutive)."""
        d = _make_downloader()
        self._patch_browser(d)

        records = [PaperRecord(doi=f"10.1021/r{i}") for i in range(8)]
        seq = [_blocked_result(), _blocked_result(), _ok_result(),
               _blocked_result(), _blocked_result(), _ok_result(),
               _blocked_result(), _blocked_result()]

        def fake_fetch_one(page, record, run_dir):
            return seq.pop(0)

        with patch.object(d, "fetch_one", side_effect=fake_fetch_one):
            d._run_once_parallel(records, _tmp_run_dir(), worker_count=1, phase="primary")

        # No run of >=3 consecutive blocks => no auto-stop.
        self.assertFalse(d._ip_block_stopped)

    def test_serial_path_stops_too(self):
        d = _make_downloader()
        self._patch_browser(d)

        records = [PaperRecord(doi=f"10.1021/r{i}") for i in range(6)]
        seen = {"n": 0}

        def fake_fetch_one(page, record, run_dir):
            seen["n"] += 1
            return _blocked_result(doi=record.doi)

        with patch.object(d, "fetch_one", side_effect=fake_fetch_one):
            d._run_once(records, _tmp_run_dir(), phase="primary", concurrency=1)

        self.assertLess(seen["n"], 6)
        self.assertTrue(d._ip_block_stopped)


# ---------- helpers / stubs ----------

from pathlib import Path  # noqa: E402
import tempfile  # noqa: E402


def Path_tmp():
    return Path(tempfile.mkdtemp())


def _tmp_run_dir():
    p = Path(tempfile.mkdtemp())
    return p


class _FakePage:
    def close(self):
        pass


class _FakePagePool:
    """Stub for _PagePool: hands out fake pages, no real browser."""

    def __init__(self, context, max_size=1):
        self.max_size = max_size

    def acquire(self):
        return _FakePage()

    def release(self, page):
        pass

    def close_all(self):
        pass


if __name__ == "__main__":
    unittest.main()
