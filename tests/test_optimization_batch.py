"""Tests for the 1.12 optimization batch: mirror health, Turnstile
interactive mode, institutional lane parallelism, SI parallelism."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scansci_pdf import pipeline
from scansci_pdf.pipeline import QueueEntry
from scansci_pdf.sources import scihub

VG_VERIFICATION = ('<html><head><title>Verification - Sci-Hub</title></head>'
                   '<body><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body></html>')
AL_HOMEPAGE = '<html><head><title>Sci-Hub - search proxy to download articles</title></head></html>'
ARTICLE = '<html><body><iframe src="//sci-hub.ru/downloads/x.pdf"></iframe></body></html>'
ALTCHA_WALL = '<html><head><title>Sci-Hub: 你是机器人吗？</title></head><body>altcha</body></html>'


class MirrorClassificationTests(unittest.TestCase):
    def test_turnstile_page(self):
        self.assertEqual(scihub._classify_mirror_page(VG_VERIFICATION), "turnstile")

    def test_homepage_shell(self):
        self.assertEqual(scihub._classify_mirror_page(AL_HOMEPAGE), "homepage")

    def test_article_not_structural(self):
        self.assertEqual(scihub._classify_mirror_page(ARTICLE), "other")

    def test_altcha_left_to_dedicated_solver(self):
        self.assertEqual(scihub._classify_mirror_page(ALTCHA_WALL), "other")


class StructuralCooldownTests(unittest.TestCase):
    def setUp(self):
        scihub._WALL_STATE.clear()
        self.addCleanup(scihub._WALL_STATE.clear)

    def test_structural_failure_skips_mirror_for_hours(self):
        scihub._note_structural("https://sci-hub.vg")
        self.assertTrue(scihub._wall_guard("https://sci-hub.vg"))
        remaining = scihub._wall_state("https://sci-hub.vg")["cooldown_until"] - time.time()
        self.assertGreater(remaining, 3600, "structural skip must be hours, not seconds")

    def test_structural_does_not_touch_other_mirrors(self):
        scihub._note_structural("https://sci-hub.vg")
        self.assertFalse(scihub._wall_guard("https://sci-hub.ru"))


import time  # noqa: E402


class HeadlessProbeTests(unittest.TestCase):
    def test_scihub_browser_headless_overrides(self):
        self.assertTrue(scihub._racing_browser_headless({"scihub_browser_headless": True}))
        self.assertFalse(scihub._racing_browser_headless({"browser_headless": False}))


class InstitutionalParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def _cfg(self, workers):
        return {"institutional_workers": workers, "download_si": False, "lane_oa_enrich": False}

    def _run(self, dois, workers, mock_cls):
        entries = [QueueEntry(identifier=d, channel="institution") for d in dois]
        with patch("scansci_pdf.institutional.fetcher.PaperFetcher", mock_cls), \
             patch("scansci_pdf.institutional.config_adapter.ConfigAdapter") as mock_cfg:
            mock_cfg.load.return_value = MagicMock(_config={})
            pipeline.run_lanes(entries, self.out, config=self._cfg(workers), allow_grey=False)

    def test_default_is_serial_one_fetcher(self):
        dois = [f"10.1/inst-{i}" for i in range(4)]
        mock_cls = MagicMock()
        mock_cls.return_value.fetch_with_result.return_value.to_dict.return_value = {
            "success": True, "doi": "x"}
        self._run(dois, 1, mock_cls)
        self.assertEqual(mock_cls.call_count, 1, "workers=1 must stay serial")

    def test_workers_two_splits_and_covers_all(self):
        dois = [f"10.1/inst-{i}" for i in range(4)]
        mock_cls = MagicMock()
        inst = mock_cls.return_value
        inst.fetch_with_result.return_value.to_dict.return_value = {"success": True, "doi": "x"}
        self._run(dois, 2, mock_cls)
        self.assertEqual(mock_cls.call_count, 2, "two fetchers, each with its own login")
        seen = [c.args[0] for c in inst.fetch_with_result.call_args_list if c.args]
        self.assertEqual(sorted(seen), sorted(dois), "all DOIs must be covered exactly once")


class SIParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_parallel_si_covers_every_successful_paper(self):
        results = [{"success": True, "doi": f"10.1/p{i}", "file": str(self.out / f"p{i}.pdf")}
                   for i in range(3)]
        with patch.object(pipeline, "fetch_supplementary",
                          return_value=[str(self.out / "a_SI1.pdf")]) as mock_fetch:
            pipeline._fetch_si_for_results(results, self.out, {"download_si": True})
        self.assertEqual(mock_fetch.call_count, 3)
        manifest = json.loads((self.out / "si_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 3)


if __name__ == "__main__":
    unittest.main()
