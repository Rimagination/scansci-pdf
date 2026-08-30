"""Reaper fallback: kill the driver process when graceful close is impossible.

At interpreter exit the ThreadPoolExecutor hook has already taken the worker
threads, so a pooled browser's close() fails cross-thread (Playwright sync
objects are thread-affine). The reaper must then kill the node driver
process — or the whole chrome tree outlives the batch (issue #19 orphans,
observed live after a 200-paper batch: 2 browsers / 10 chrome processes).
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scansci_pdf import browser_engine as be


class _FakeProc:
    def __init__(self):
        self.killed = False

    def poll(self):
        return None  # still running

    def kill(self):
        self.killed = True


class _BrokenBrowser:
    """A pooled browser whose owner thread is gone: close() fails."""

    def __init__(self, proc):
        self._proc = proc

    def close(self):
        raise RuntimeError("cannot switch to another thread")

    class _impl_obj:
        class _connection:
            class _transport:
                _proc = None  # patched per test


def _make_broken(proc):
    b = _BrokenBrowser(proc)
    b._impl_obj = type("impl", (), {})()
    b._impl_obj._connection = type("conn", (), {})()
    b._impl_obj._connection._transport = type("tr", (), {})()
    b._impl_obj._connection._transport._proc = proc
    return b


class ReaperTests(unittest.TestCase):
    def setUp(self):
        be._LIVE_BROWSERS.clear()

    def tearDown(self):
        be._LIVE_BROWSERS.clear()

    def test_kill_fallback_when_close_fails(self):
        proc = _FakeProc()
        b = _make_broken(proc)
        be._LIVE_BROWSERS.add(b)
        be._reap_browsers_at_exit()
        self.assertTrue(proc.killed, "driver process must be killed on failed close")
        self.assertNotIn(b, be._LIVE_BROWSERS)

    def test_graceful_close_no_kill(self):
        proc = _FakeProc()
        b = Mock()
        b.close.return_value = None
        be._LIVE_BROWSERS.add(b)
        be._reap_browsers_at_exit()
        b.close.assert_called_once()
        self.assertFalse(proc.killed)

    def test_dead_driver_not_touched(self):
        proc = _FakeProc()
        proc.poll = lambda: 0  # already exited
        b = _make_broken(proc)
        be._LIVE_BROWSERS.add(b)
        be._reap_browsers_at_exit()
        self.assertFalse(proc.killed, "a dead driver must not be killed again")

    def test_real_browser_lifecycle_end_to_end(self):
        """Real patchright launch: registry captures the browser, graceful
        close via the reaper works while the owning thread is alive."""
        if not be.is_available({}):
            self.skipTest("no browser backend installed")
        cfg = {"browser_backend": "patchright", "browser_headless": True}
        b, _ = be._get_shared_browser(cfg)
        self.assertIn(b, be._LIVE_BROWSERS)
        be._reap_browsers_at_exit()
        self.assertNotIn(b, be._LIVE_BROWSERS)
        self.assertFalse(b.is_connected(), "browser must be closed after reaping")


if __name__ == "__main__":
    unittest.main()
