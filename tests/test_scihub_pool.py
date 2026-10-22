"""Tests for the persistent Sci-Hub browser-worker pool.

The pool's whole point: its threads live for the process lifetime, so each
worker's thread-local browser (browser_engine._get_shared_browser) is created
once and reused for every paper — no per-paper window flashing. These tests
verify the properties that make that work, without launching real browsers.
"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from scansci_pdf.sources import scihub


def _own_close_calls(pool, calls):
    """The close calls made by `pool`'s own workers.

    _close_my_browser resolves `shutdown_shared_browser` when the task *runs*,
    not when it is queued, and _shutdown_scihub_pool queues the close tasks
    with shutdown(wait=False). A task queued by an earlier test in this module
    can therefore still be in flight while a later test measures its own
    shutdown — and when it lands it resolves the very same patched attribute.
    Only the workers of the pool under test may be counted against its bound.
    `pool._threads` is the executor's worker thread set (the module already
    relies on the executor's `pool._max_workers`).
    """
    return [t for t in calls if t in pool._threads]


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
            # The close tasks are queued with shutdown(wait=False) on purpose
            # (issue #57: a worker stuck in an interactive Cloudflare wait must
            # not hang process exit), so they land later, in the pool's
            # workers. The patch has to stay installed until they have run:
            # _close_my_browser resolves shutdown_shared_browser when the task
            # runs, so a task that starts after the patch is gone calls the
            # real function and would never reach `calls` at all.
            #
            # Joining the workers is the deterministic way to get there: the
            # close tasks are queued ahead of the executor's shutdown
            # sentinel, and a worker only exits after it has run them. So the
            # join means every close task has run — provided no worker is
            # stuck for longer than the join timeout, in which case the
            # assertions below fail rather than the test hanging. Polling for
            # "the first call" instead would snapshot the count at an
            # arbitrary moment, which is how the bound below used to pass
            # without ever being enforceable.
            for thread in list(pool._threads):
                thread.join(timeout=5)

            own_calls = _own_close_calls(pool, calls)

        self.assertIsNone(scihub._RACE_POOL)
        # The product queues one close task per configured slot, and the
        # join above means every one of them has run.
        self.assertEqual(
            len(own_calls),
            pool._max_workers,
            "the pool's workers did not close one browser each",
        )
        # The executor is stopped: the workers are gone. Without the
        # shutdown sentinel they would still be running, and the join above
        # would have timed out silently.
        self.assertFalse(
            [thread for thread in pool._threads if thread.is_alive()],
            "the pool's workers were still alive after the shutdown",
        )
        # A close must never run on the main thread: Playwright's sync objects
        # are thread-affine, so the browser has to be closed by the worker that
        # owns it. Counting only this pool's workers would hide such a close,
        # so it is checked separately.
        self.assertNotIn(
            threading.main_thread(),
            calls,
            "a close ran on the main thread instead of in the pool worker",
        )

    def test_a_close_task_queued_by_another_pool_is_not_counted_against_this_pool(self):
        """Regression: the shutdown measurement must not see another pool's
        close task.

        _close_my_browser resolves `shutdown_shared_browser` when the task runs,
        not when it is queued. A close task queued by an earlier test in this
        module can still be in flight while this test measures its own
        shutdown, and it resolves the same patched attribute when it lands.
        Counting it against this pool breaks the bound above — that is how the
        macOS CI job failed with "3 not less than or equal to 2".
        """
        foreign = ThreadPoolExecutor(max_workers=1, thread_name_prefix="foreign")
        release = threading.Event()
        try:
            # Occupy the only foreign worker, then queue a close task behind
            # it, so the task is guaranteed to land late — i.e. while this
            # test's patch is installed.
            foreign.submit(release.wait)
            foreign.submit(scihub._close_my_browser)

            # The pool under test needs a real worker of its own, otherwise
            # there is nothing for the filter to match against and every call
            # would count as foreign. It is deliberately left for the class
            # cleanup to shut down; the close tasks that cleanup queues land
            # asynchronously, and the filter is what keeps them out of a
            # later test's measurement.
            pool = scihub._race_pool({"scihub_browser_workers": 2})
            pool.submit(threading.get_ident).result()
            calls: list[threading.Thread] = []

            def fake_close():
                calls.append(threading.current_thread())

            with patch("scansci_pdf.browser_engine.shutdown_shared_browser", fake_close):
                release.set()
                # Shut the foreign pool down so its worker exits after the
                # queued close task instead of blocking on the next item.
                foreign.shutdown(wait=False)
                for thread in list(foreign._threads):
                    thread.join(timeout=5)
                # The other direction, in the same patch window: a close issued
                # by this pool's own worker is counted.
                pool.submit(scihub._close_my_browser).result()

            # The foreign task really did resolve the patched attribute ...
            # ... and it is not counted against this pool. The counts are
            # relative to this pool on purpose: a close task queued by an
            # earlier test can still be in flight and lands in the same
            # list, and an absolute count would make this test fail for a
            # reason that has nothing to do with it.
            own = _own_close_calls(pool, calls)
            self.assertEqual(
                len(own), 1, "the pool's own close was not counted"
            )
            self.assertGreaterEqual(
                len(calls), 2, "the foreign task never resolved the patched attribute"
            )
        finally:
            release.set()
            foreign.shutdown(wait=False)

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
