"""Tests for verification-wall pacing (ALTCHA gates like sci-hub.ru).

Live experiments (2026-08-30): the gate escalates with request velocity —
solving works once, then rapid repeat verifications earn a standing
"你是机器人吗" wall. The state machine spaces solves out and cools a domain
down exponentially when a wall persists, instead of hammering.
"""

import time
import unittest
from unittest.mock import patch

from scansci_pdf.sources import scihub


class WallPacingTests(unittest.TestCase):
    def setUp(self):
        scihub._WALL_STATE.clear()

    def tearDown(self):
        scihub._WALL_STATE.clear()

    def test_fresh_domain_not_guarded(self):
        self.assertFalse(scihub._wall_guard("https://sci-hub.ru"))

    def test_note_wall_triggers_cooldown(self):
        scihub._note_wall("https://sci-hub.ru")
        self.assertTrue(scihub._wall_guard("https://sci-hub.ru"))
        self.assertEqual(scihub._wall_state("https://sci-hub.ru")["walls"], 1)

    def test_cooldown_escalates_exponentially(self):
        scihub._note_wall("https://sci-hub.ru")
        c1 = scihub._wall_state("https://sci-hub.ru")["cooldown_until"] - time.time()
        scihub._note_wall("https://sci-hub.ru")
        c2 = scihub._wall_state("https://sci-hub.ru")["cooldown_until"] - time.time()
        self.assertGreater(c2, c1 * 2, "second wall must cool down longer")

    def test_cooldown_is_capped(self):
        for _ in range(10):
            scihub._note_wall("https://sci-hub.ru")
        remaining = scihub._wall_state("https://sci-hub.ru")["cooldown_until"] - time.time()
        self.assertLessEqual(remaining, scihub._WALL_COOLDOWN_CAP_SEC + 5)

    def test_success_resets_state(self):
        scihub._note_wall("https://sci-hub.ru")
        scihub._note_wall("https://sci-hub.ru")
        scihub._note_wall_success("https://sci-hub.ru")
        st = scihub._wall_state("https://sci-hub.ru")
        self.assertEqual(st["walls"], 0)
        self.assertEqual(st["cooldown_until"], 0.0)

    def test_guard_expires_after_cooldown(self):
        scihub._note_wall("https://sci-hub.ru")
        # fast-forward past the cooldown
        st = scihub._wall_state("https://sci-hub.ru")
        st["cooldown_until"] = time.time() - 1
        self.assertFalse(scihub._wall_guard("https://sci-hub.ru"))

    def test_pace_sleeps_when_too_soon(self):
        scihub._note_wall_success("https://sci-hub.ru")  # sets last_solve = now
        with patch.object(scihub.time, "sleep") as mock_sleep:
            scihub._wall_pace("https://sci-hub.ru")
        self.assertTrue(mock_sleep.called, "must sleep to space out verifications")

    def test_pace_no_sleep_when_spaced(self):
        st = scihub._wall_state("https://sci-hub.ru")
        st["last_solve"] = time.time() - scihub._WALL_MIN_SPACING_SEC * 2
        with patch.object(scihub.time, "sleep") as mock_sleep:
            scihub._wall_pace("https://sci-hub.ru")
        self.assertFalse(mock_sleep.called)

    def test_domains_are_independent(self):
        scihub._note_wall("https://sci-hub.ru")
        self.assertFalse(scihub._wall_guard("https://sci-hub.vg"))


if __name__ == "__main__":
    unittest.main()
