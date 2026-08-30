"""Tests for browser-context recycling (``browser_restart_every``).

Long runs (thousands of papers) drift in memory inside a single Chrome
context. ``browser_restart_every`` segments ``run_records`` so the context is
closed and relaunched every N records; cookies are handed over in memory
because session cookies (no Expires) do not survive a persistent-context
restart on disk.
"""

import tempfile
import unittest
from pathlib import Path

from scansci_pdf.publisher_batch import (
    DownloadResult,
    PaperRecord,
    PublisherBatchDownloader,
)


class _FakeContext:
    """Records add_cookies/close calls; serves canned cookies()."""

    def __init__(self, name: str):
        self.name = name
        self.added: list[list[dict]] = []
        self.closed = False

    def cookies(self):
        return [{"name": f"cookie-from-{self.name}", "value": "1"}]

    def add_cookies(self, cookies):
        self.added.append(list(cookies))

    def close(self):
        self.closed = True


class _FakeProfile:
    name = "TEST"


def _make_downloader(config: dict | None = None) -> PublisherBatchDownloader:
    d = PublisherBatchDownloader.__new__(PublisherBatchDownloader)
    d.config = config or {}
    d.profile = _FakeProfile()
    d.institution_query = ""
    d.login_timeout_sec = 0
    d.pdf_timeout_ms = 1000
    d.post_login_hold_sec = 0
    d.post_run_hold_sec = 0
    d._ip_block_stopped = False
    d._proxy_blocked = []
    d._context_proxy = {}
    d._handoff_cookies = None
    return d


def _records(n: int) -> list[PaperRecord]:
    return [PaperRecord(doi=f"10.1000/test-{i}") for i in range(n)]


class BrowserRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "run"
        self.contexts: list[_FakeContext] = []

    def _launch_context(self, profile_dir=None, *, proxy=None):
        ctx = _FakeContext(f"ctx{len(self.contexts)}")
        self.contexts.append(ctx)
        return ctx

    def _run(self, n: int, restart_every: int) -> dict:
        d = _make_downloader({"browser_restart_every": restart_every})
        d._launch_context = self._launch_context
        d.fetch_one = lambda ctx, rec, run_dir: DownloadResult(
            doi=rec.doi, status="success", verified_match=True
        )
        return d.run_records(_records(n), self.run_dir)

    def test_context_recycled_every_n_records(self):
        summary = self._run(5, restart_every=2)
        self.assertEqual(len(self.contexts), 3)  # segments of 2 + 2 + 1
        self.assertTrue(all(c.closed for c in self.contexts))
        self.assertEqual(summary["final"]["success"], 5)

    def test_cookies_handed_over_between_contexts(self):
        self._run(5, restart_every=2)
        # Contexts 2 and 3 must receive the previous context's cookies; the
        # first context starts clean.
        self.assertEqual(self.contexts[0].added, [])
        self.assertEqual(
            self.contexts[1].added,
            [[{"name": "cookie-from-ctx0", "value": "1"}]],
        )
        self.assertEqual(
            self.contexts[2].added,
            [[{"name": "cookie-from-ctx1", "value": "1"}]],
        )

    def test_restart_disabled_keeps_single_context(self):
        self._run(4, restart_every=0)
        self.assertEqual(len(self.contexts), 1)

    def test_restart_at_or_above_batch_size_keeps_single_context(self):
        self._run(3, restart_every=10)
        self.assertEqual(len(self.contexts), 1)

    def test_ip_block_stop_halts_remaining_segments(self):
        d = _make_downloader({"browser_restart_every": 4})
        d._launch_context = self._launch_context

        def blocked(ctx, rec, run_dir):
            return DownloadResult(doi=rec.doi, status="failed", reason="ip_blocked")

        d.fetch_one = blocked
        summary = d.run_records(_records(12), self.run_dir)
        # The first segment (4 records) trips the 3-consecutive-block stop;
        # the remaining two segments must not launch a new context.
        self.assertEqual(len(self.contexts), 1)
        self.assertTrue(summary.get("auto_stopped"))


if __name__ == "__main__":
    unittest.main()
