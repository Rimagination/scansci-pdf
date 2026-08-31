"""Tests for verification-wall pacing (ALTCHA gates like sci-hub.ru).

Live experiments (2026-08-30): the gate escalates with request VELOCITY —
solving works once, then rapid repeat verifications earn a standing
"你是机器人吗" wall. State is persisted in domain_db's wall_state table so
cooldowns survive process restarts (three earlier generations of in-memory
or parallel health stores caused rework; do not reintroduce them).
"""

import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from scansci_pdf import domain_db
from scansci_pdf.sources import scihub


def _cfg():
    return {"cache_dir": str(Path(_tmp.name) / "cache")}


_tmp = tempfile.TemporaryDirectory()


class WallPacingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        domain_db.reset_wall_state(self.cfg)
        self.addCleanup(lambda: (domain_db.close_connection(), domain_db.reset_wall_state(self.cfg)))

    def test_fresh_domain_not_guarded(self):
        self.assertFalse(scihub._wall_guard("https://sci-hub.ru", self.cfg))

    def test_note_wall_triggers_cooldown(self):
        scihub._note_wall("https://sci-hub.ru", self.cfg)
        self.assertTrue(scihub._wall_guard("https://sci-hub.ru", self.cfg))
        self.assertEqual(domain_db.get_wall_state("https://sci-hub.ru", self.cfg)["walls"], 1)

    def test_cooldown_escalates_exponentially(self):
        scihub._note_wall("https://sci-hub.ru", self.cfg)
        c1 = domain_db.get_wall_state("https://sci-hub.ru", self.cfg)["cooldown_until"] - time.time()
        scihub._note_wall("https://sci-hub.ru", self.cfg)
        c2 = domain_db.get_wall_state("https://sci-hub.ru", self.cfg)["cooldown_until"] - time.time()
        self.assertGreater(c2, c1 * 2, "second wall must cool down longer")

    def test_cooldown_is_capped(self):
        for _ in range(10):
            scihub._note_wall("https://sci-hub.ru", self.cfg)
        remaining = domain_db.get_wall_state("https://sci-hub.ru", self.cfg)["cooldown_until"] - time.time()
        self.assertLessEqual(remaining, scihub._WALL_COOLDOWN_CAP_SEC + 5)

    def test_success_resets_state(self):
        scihub._note_wall("https://sci-hub.ru", self.cfg)
        scihub._note_wall_success("https://sci-hub.ru", self.cfg)
        st = domain_db.get_wall_state("https://sci-hub.ru", self.cfg)
        self.assertEqual(st["walls"], 0)
        self.assertEqual(st["cooldown_until"], 0.0)

    def test_guard_expires_after_cooldown(self):
        scihub._note_wall("https://sci-hub.ru", self.cfg)
        domain_db.set_wall_state("https://sci-hub.ru", self.cfg,
                                 last_solve=time.time(), walls=1, cooldown_until=time.time() - 1)
        self.assertFalse(scihub._wall_guard("https://sci-hub.ru", self.cfg))

    def test_pace_sleeps_when_too_soon(self):
        scihub._note_wall_success("https://sci-hub.ru", self.cfg)  # sets last_solve = now
        with patch.object(scihub.time, "sleep") as mock_sleep:
            scihub._wall_pace("https://sci-hub.ru", self.cfg)
        self.assertTrue(mock_sleep.called, "must sleep to space out verifications")

    def test_pace_no_sleep_when_spaced(self):
        domain_db.set_wall_state("https://sci-hub.ru", self.cfg,
                                 last_solve=time.time() - scihub._WALL_MIN_SPACING_SEC * 2,
                                 walls=0, cooldown_until=0)
        with patch.object(scihub.time, "sleep") as mock_sleep:
            scihub._wall_pace("https://sci-hub.ru", self.cfg)
        self.assertFalse(mock_sleep.called)

    def test_domains_are_independent(self):
        scihub._note_wall("https://sci-hub.ru", self.cfg)
        self.assertFalse(scihub._wall_guard("https://sci-hub.vg", self.cfg))

    def test_state_survives_connection_close(self):
        """The whole point of the sqlite home: cooldowns outlive the process."""
        scihub._note_wall("https://sci-hub.ru", self.cfg)
        domain_db.close_connection()  # simulate process exit
        self.assertTrue(scihub._wall_guard("https://sci-hub.ru", self.cfg))


if __name__ == "__main__":
    unittest.main()
