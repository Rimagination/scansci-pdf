"""Tests for the auto-launching progress bar (ensure_progress_bar).

Every task start pops the floating bar once: independent process, pid-lock
guards against duplicates, stale locks reclaimed, kill switches honored.
The Windows liveness probe must NEVER use os.kill(pid, 0) - there it calls
TerminateProcess and would murder the bar it was checking.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scansci_pdf import progress_reporter as pr


def _enabled_env():
    return {"SCANSCI_PROGRESS_BAR": "1", "SCANSCI_PROGRESS_BAR_CHILD": "0"}


class PidAliveTests(unittest.TestCase):
    def test_own_pid_alive(self):
        self.assertTrue(pr._pid_alive(os.getpid()))

    def test_dead_pid_not_alive(self):
        self.assertFalse(pr._pid_alive(999999))

    def test_zero_and_negative_rejected(self):
        self.assertFalse(pr._pid_alive(0))
        self.assertFalse(pr._pid_alive(-5))


class EnsureProgressBarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"progress_bar_auto": True}
        patcher = patch.object(pr, "_progress_dir",
                               return_value=Path(self.tmp.name) / "progress")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lock(self, pid):
        lock = Path(self.tmp.name) / "progress" / "bar.pid"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(pid), encoding="utf-8")
        return lock

    def test_spawns_when_no_lock(self):
        with patch.dict(os.environ, _enabled_env()), \
             patch("subprocess.Popen") as popen:
            pr.ensure_progress_bar(self.cfg)
        popen.assert_called_once()
        argv = popen.call_args[0][0]
        self.assertIn("scansci_pdf.progress_bar", " ".join(argv))

    def test_no_spawn_when_bar_alive(self):
        self._lock(os.getpid())  # a very alive pid
        with patch.dict(os.environ, _enabled_env()), \
             patch("subprocess.Popen") as popen:
            pr.ensure_progress_bar(self.cfg)
        popen.assert_not_called()

    def test_stale_lock_reclaimed(self):
        self._lock(999999)  # dead bar
        with patch.dict(os.environ, _enabled_env()), \
             patch("subprocess.Popen") as popen:
            pr.ensure_progress_bar(self.cfg)
        popen.assert_called_once()

    def test_env_kill_switch(self):
        env = dict(_enabled_env(), SCANSCI_PROGRESS_BAR="0")
        with patch.dict(os.environ, env), \
             patch("subprocess.Popen") as popen:
            pr.ensure_progress_bar(self.cfg)
        popen.assert_not_called()

    def test_config_kill_switch(self):
        cfg = dict(self.cfg, progress_bar_auto=False)
        with patch.dict(os.environ, _enabled_env()), \
             patch("subprocess.Popen") as popen:
            pr.ensure_progress_bar(cfg)
        popen.assert_not_called()

    def test_child_flag_prevents_recursion(self):
        env = dict(_enabled_env(), SCANSCI_PROGRESS_BAR_CHILD="1")
        with patch.dict(os.environ, env), \
             patch("subprocess.Popen") as popen:
            pr.ensure_progress_bar(self.cfg)
        popen.assert_not_called()


class StartTaskAutoLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"progress_bar_auto": True}
        patcher = patch.object(pr, "_progress_dir",
                               return_value=Path(self.tmp.name) / "progress")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: setattr(pr, "_STATE", {}))

    def test_start_task_spawns_bar_and_writes_state(self):
        with patch.dict(os.environ, _enabled_env()), \
             patch("subprocess.Popen") as popen, \
             patch("scansci_pdf.config.load_config", return_value=self.cfg):
            pr.start_task("文献下载", total=10)
        popen.assert_called_once()
        state = json.loads((Path(self.tmp.name) / "progress" / "current.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(state["task"], "文献下载")
        self.assertEqual(state["total"], 10)


if __name__ == "__main__":
    unittest.main()
