"""Tests for the persistent Sci-Hub browser-worker pool.

The pool's whole point: its threads live for the process lifetime, so each
worker's thread-local browser (browser_engine._get_shared_browser) is created
once and reused for every paper — no per-paper window flashing. These tests
verify the properties that make that work, without launching real browsers.
"""

import threading
import unittest
from unittest.mock import patch

from scansci_pdf.sources import scihub


class PersistentPoolTests(unittest.TestCase):
    def setUp(self):
        # Start every test from a clean slate (pools are module-global).
        scihub._shutdown_scihub_pool()
        self.addCleanup(scihub._shutdown_scihub_pool)

    def test_pool_is_singleton(self):
        p1 = scihub._race_pool({})
        p2 = scihub._race_pool({})
        self.assertIs(p1, p2)

    def test_pool_size_from_config(self):
        pool = scihub._race_pool({"scihub_browser_workers": 2})
        self.assertEqual(pool._max_workers, 2)

    def test_pool_size_fallback_default(self):
        pool = scihub._race_pool({})
        self.assertEqual(pool._max_workers, 3)

    def test_worker_threads_are_reused(self):
        """Few distinct threads across many submissions — the property that
        lets thread-local browsers persist across papers."""
        pool = scihub._race_pool({"scihub_browser_workers": 2})
        idents = {pool.submit(threading.get_ident).result() for _ in range(12)}
        self.assertLessEqual(len(idents), 2, "workers must be reused, not respawned")

    def test_shutdown_resets_pool_and_closes_worker_browsers(self):
        pool = scihub._race_pool({"scihub_browser_workers": 2})
        pool.submit(threading.get_ident).result()  # ensure a worker exists
        calls: list[threading.Thread] = []

        def fake_close():
            calls.append(threading.current_thread())

        with patch("scansci_pdf.browser_engine.shutdown_shared_browser", fake_close):
            # _close_my_browser imports lazily inside the worker; patch the
            # resolved module attribute it will find.
            scihub._shutdown_scihub_pool()

        self.assertIsNone(scihub._RACE_POOL)
        # Each worker closed its own browser in its own thread.
        self.assertLessEqual(len(calls), 2)
        self.assertTrue(calls)

    def test_shutdown_is_idempotent(self):
        scihub._shutdown_scihub_pool()
        scihub._shutdown_scihub_pool()  # must not raise
        self.assertIsNone(scihub._RACE_POOL)

    def test_new_pool_after_shutdown(self):
        p1 = scihub._race_pool({})
        scihub._shutdown_scihub_pool()
        p2 = scihub._race_pool({})
        self.assertIsNot(p1, p2)

    def test_browser_available_stays_true_in_pool_worker_after_launch(self):
        """Regression: launching the sync API leaves its dispatcher event loop
        running in the worker thread. The naive asyncio guard used to read
        that as 'user asyncio code' and made _is_browser_available return
        False for every paper after the first — silently degrading whole
        batches to HTTP-only."""
        from scansci_pdf import browser_engine as be

        if not be.is_available({}):
            self.skipTest("no browser backend installed")
        pool = scihub._race_pool({"scihub_browser_workers": 1})
        cfg = {"browser_backend": "patchright", "browser_headless": True}
        try:
            first = pool.submit(scihub._is_browser_available, {"scihub_enabled": True}).result()
            pool.submit(be._get_shared_browser, cfg).result()  # launch in worker
            second = pool.submit(scihub._is_browser_available, {"scihub_enabled": True}).result()
            self.assertTrue(first)
            self.assertTrue(
                second,
                "browser availability must not flip off after the first launch",
            )
        finally:
            pool.submit(scihub._close_my_browser).result()


if __name__ == "__main__":
    unittest.main()
