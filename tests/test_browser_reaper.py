"""Reaper fallback: tree-kill the driver when graceful close is impossible.

At interpreter exit the ThreadPoolExecutor hook has already taken the worker
threads, so a pooled browser's close() fails cross-thread (Playwright sync
objects are thread-affine). Worse, on Windows killing the node driver alone
leaves its chrome children alive - the reaper must tree-kill. Observed live
after a 200-paper batch: 2 browsers / 10 chrome processes survived.
"""

import unittest
from unittest.mock import Mock, patch

from scansci_pdf import browser_engine as be


def _make_broken(proc):
    """A pooled browser whose owner thread is gone: close() fails, but the
    driver process handle is reachable (what the reaper needs)."""
    b = Mock()
    b.close.side_effect = RuntimeError("cannot switch to another thread")
    b._impl_obj._connection._transport._proc = proc
    return b


class TreeKillTests(unittest.TestCase):
    def test_skips_dead_driver(self):
        proc = Mock()
        proc.poll.return_value = 1  # already exited
        be._tree_kill(proc)
        proc.kill.assert_not_called()

    def test_windows_uses_taskkill_tree(self):
        proc = Mock()
        proc.poll.return_value = None
        proc.pid = 4242
        with patch("scansci_pdf.browser_engine.subprocess.run") as run:
            be._tree_kill(proc)
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["taskkill", "/F", "/T"])
        self.assertEqual(args[3:], ["/PID", "4242"])

    def test_posix_uses_proc_kill(self):
        proc = Mock()
        proc.poll.return_value = None
        with patch("scansci_pdf.browser_engine.os") as mock_os, \
             patch("scansci_pdf.browser_engine.subprocess.run") as run:
            mock_os.name = "posix"
            be._tree_kill(proc)
        proc.kill.assert_called_once()
        run.assert_not_called()


class ReaperTests(unittest.TestCase):
    def setUp(self):
        be._LIVE_BROWSERS.clear()
        self.addCleanup(be._LIVE_BROWSERS.clear)

    def test_graceful_close_no_kill(self):
        b = Mock()
        be._LIVE_BROWSERS.add(b)
        with patch("scansci_pdf.browser_engine._tree_kill") as tk:
            be._reap_browsers_at_exit()
        b.close.assert_called_once()
        tk.assert_called_once()  # verify-then-kill backstop still checks

    def test_close_failure_still_tree_kills(self):
        proc = Mock()
        proc.poll.return_value = None
        b = _make_broken(proc)
        be._LIVE_BROWSERS.add(b)
        with patch("scansci_pdf.browser_engine._tree_kill") as tk:
            be._reap_browsers_at_exit()
        tk.assert_called_once_with(proc)

    def test_registry_cleared(self):
        b = Mock()
        be._LIVE_BROWSERS.add(b)
        be._reap_browsers_at_exit()
        self.assertEqual(be._LIVE_BROWSERS, set())


class RealBrowserTests(unittest.TestCase):
    def test_real_browser_reaped(self):
        """Real patchright launch: after reaping, the browser is disconnected."""
        if not be.is_available({}):
            self.skipTest("no browser backend installed")
        cfg = {"browser_backend": "patchright", "browser_headless": True}
        b, _ = be._get_shared_browser(cfg)
        self.assertIn(b, be._LIVE_BROWSERS)
        be._reap_browsers_at_exit()
        self.assertNotIn(b, be._LIVE_BROWSERS)
        self.assertFalse(b.is_connected())


if __name__ == "__main__":
    unittest.main()
