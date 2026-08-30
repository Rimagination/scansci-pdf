"""Tests for lane-scheduling OA enrichment (``_enrich_oa_urls``).

Root cause being fixed: the channel-prefix DB only knows Elsevier, so plain
DOI lists sent OA journals (NAR, PLoS, …) down the grey or institutional
lanes — into publisher bot-walls needing manual Turnstile clicks — even when
OpenAlex has a direct PDF URL. Enrichment routes those into the fast lane
before scheduling; every failure path must leave entries untouched.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scansci_pdf import pipeline
from scansci_pdf.pipeline import QueueEntry, _enrich_oa_urls, _looks_like_pdf_url


def _cfg(tmp: Path) -> dict:
    return {
        "cache_dir": str(tmp / "cache"),
        "cache_ttl_hours": 168,
        "lane_oa_enrich": True,
        "email": "test@example.invalid",
        "network_proxy": "",
    }


class LooksLikePdfUrlTests(unittest.TestCase):
    def test_oup_article_pdf(self):
        self.assertTrue(_looks_like_pdf_url(
            "https://academic.oup.com/nar/article-pdf/51/D1/D523/48441158/gkac1052.pdf"))

    def test_doi_org_landing_is_not_pdf(self):
        self.assertFalse(_looks_like_pdf_url("https://doi.org/10.1093/nar/gkac1052"))

    def test_empty(self):
        self.assertFalse(_looks_like_pdf_url(""))


class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(Path(self.tmp.name))

    def test_enriches_auto_entry_with_oa_pdf(self):
        entries = [QueueEntry(identifier="10.1093/nar/gkac1052")]
        with patch.object(pipeline, "_fetch_oa_pdf",
                          return_value="https://academic.oup.com/nar/article-pdf/x.pdf"):
            _enrich_oa_urls(entries, self.cfg)
        self.assertEqual(entries[0].oa_url, "https://academic.oup.com/nar/article-pdf/x.pdf")
        self.assertEqual(entries[0].channel, "oa")

    def test_no_pdf_url_leaves_entry_untouched(self):
        entries = [QueueEntry(identifier="10.1234/paywalled")]
        with patch.object(pipeline, "_fetch_oa_pdf", return_value=""):
            _enrich_oa_urls(entries, self.cfg)
        self.assertEqual(entries[0].oa_url, "")
        self.assertEqual(entries[0].channel, "auto")

    def test_network_failure_leaves_entry_untouched(self):
        entries = [QueueEntry(identifier="10.1234/broken")]
        with patch.object(pipeline, "_fetch_oa_pdf", side_effect=RuntimeError("down")):
            _enrich_oa_urls(entries, self.cfg)
        self.assertEqual(entries[0].oa_url, "")

    def test_elsevier_channel_not_enriched(self):
        entries = [QueueEntry(identifier="10.1016/j.watres.2023.121036", channel="elsevier")]
        calls = []
        with patch.object(pipeline, "_fetch_oa_pdf",
                          side_effect=lambda doi, cfg: calls.append(doi) or ""):
            _enrich_oa_urls(entries, self.cfg)
        self.assertEqual(calls, [], "elsevier entries already have a fast lane")
        self.assertEqual(entries[0].channel, "elsevier")

    def test_existing_oa_url_not_overwritten(self):
        entries = [QueueEntry(identifier="10.1234/x", oa_url="https://example.org/a.pdf")]
        with patch.object(pipeline, "_fetch_oa_pdf",
                          return_value="https://other.example.org/b.pdf"):
            _enrich_oa_urls(entries, self.cfg)
        self.assertEqual(entries[0].oa_url, "https://example.org/a.pdf")

    def test_kill_switch(self):
        entries = [QueueEntry(identifier="10.1234/x")]
        self.cfg["lane_oa_enrich"] = False
        with patch.object(pipeline, "_fetch_oa_pdf",
                          side_effect=AssertionError("must not be called")):
            _enrich_oa_urls(entries, self.cfg)
        self.assertEqual(entries[0].oa_url, "")

    def test_negative_result_is_cached(self):
        """The real _fetch_oa_pdf caches 'no OA' too — a second batch must not
        re-query OpenAlex."""
        import requests

        entries = [QueueEntry(identifier="10.1234/neg")]
        resp = Mock(status_code=404)
        resp.json.return_value = {}
        with patch.object(requests, "get", return_value=resp) as mock_get:
            _enrich_oa_urls(entries, self.cfg)
            _enrich_oa_urls(entries, self.cfg)
        # 404 is a definitive negative (DOI not in the index): one request,
        # no fallback, cached — batch 2 is fully served from disk.
        self.assertEqual(mock_get.call_count, 1, "second batch must hit the disk cache")
        cached_file = list((Path(self.tmp.name) / "cache").glob("*.json"))[0]
        self.assertEqual(json.loads(cached_file.read_text(encoding="utf-8"))["pdf_url"], "")

    def test_falls_back_to_unpaywall_on_openalex_timeout(self):
        import requests

        entries = [QueueEntry(identifier="10.1093/nar/gkac1052")]
        pdf_resp = Mock(status_code=200)
        pdf_resp.json.return_value = {"best_oa_location":
                                      {"url_for_pdf": "https://academic.oup.com/nar/article-pdf/x.pdf"}}
        with patch.object(requests, "get",
                          side_effect=[requests.exceptions.Timeout("t"), pdf_resp]):
            _enrich_oa_urls(entries, self.cfg)
        self.assertEqual(entries[0].oa_url, "https://academic.oup.com/nar/article-pdf/x.pdf")
        self.assertEqual(entries[0].channel, "oa")


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_enriched_entries_land_in_fast_lane(self):
        entries = [
            QueueEntry(identifier="10.1093/nar/gkac1052"),
            QueueEntry(identifier="10.1234/nope"),
        ]

        def fake_fetch(doi, cfg):
            return ("https://academic.oup.com/nar/article-pdf/x.pdf"
                    if "nar" in doi else "")

        seen = {}

        def fake_fast(fast_entries, out, config, workers=8, progress=None):
            seen["fast"] = [e.identifier for e in fast_entries]
            return []

        with patch.object(pipeline, "_fetch_oa_pdf", side_effect=fake_fetch), \
             patch.object(pipeline, "_run_fast_lane", side_effect=fake_fast), \
             patch("scansci_pdf.sources.batch_download", return_value=[]):
            pipeline.run_lanes(entries, Path(self.tmp.name), config=_cfg(Path(self.tmp.name)))

        self.assertEqual(seen["fast"], ["10.1093/nar/gkac1052"],
                         "the OA-enriched DOI must ride the fast lane")


if __name__ == "__main__":
    unittest.main()
