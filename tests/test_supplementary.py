"""Tests for supplementary (SI) handling: link extraction, download, batch wiring.

Contract: SI failure never fails the main download; discovery is
marker+extension based with the main PDF excluded; batch wiring is gated by
the download_si config switch (default off) and writes si_manifest.json.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scansci_pdf import pipeline
from scansci_pdf.pipeline import QueueEntry
from scansci_pdf.supplementary import extract_supplementary_links, fetch_supplementary

HTML = """
<html><body>
<a href="https://www.nature.com/articles/41586_2023_MOESM1_ESM.pdf">SI 1</a>
<a href="/articles/41586_2023_MOESM2_ESM.xlsx">SI 2</a>
<a href="/articles/41586_2023_main.pdf">main pdf</a>
<a href="https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0123456.s001&type=supplementary-file">plos si</a>
<a href="/about">about</a>
</body></html>
"""

BASE = "https://www.nature.com/articles/41586_2023"


class LinkExtractionTests(unittest.TestCase):
    def test_finds_markers_and_joins_relative(self):
        links = extract_supplementary_links(HTML, BASE)
        self.assertEqual(len(links), 3)
        self.assertTrue(links[1].startswith("https://www.nature.com/articles/"))

    def test_main_pdf_excluded(self):
        self.assertFalse(any("main.pdf" in u for u in extract_supplementary_links(HTML, BASE)))

    def test_strong_marker_without_extension(self):
        self.assertTrue(any("s001" in u for u in extract_supplementary_links(HTML, BASE)))

    def test_max_files_cap(self):
        many = "".join(f'<a href="/a/mmc{i}.zip">x</a>' for i in range(30))
        self.assertEqual(len(extract_supplementary_links(many, BASE, max_files=5)), 5)

    def test_dedupe(self):
        dup = '<a href="/a/mmc1.zip">x</a><a href="/a/mmc1.zip">y</a>'
        self.assertEqual(len(extract_supplementary_links(dup, BASE)), 1)


class FetchSupplementaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_saves_with_si_names_and_skips_html(self):
        def fake_fetch(url):
            if url.endswith("MOESM1_ESM.pdf"):
                return b"<html>gate page</html>", "text/html"  # gate, not attachment
            if url.endswith(".xlsx"):
                return b"PK", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            return b"%PDF-1.4 fake", "application/pdf"

        with patch("scansci_pdf.supplementary._fetch_page", return_value=(HTML, fake_fetch)), \
             patch("scansci_pdf.supplementary._article_url", return_value=BASE):
            saved = fetch_supplementary("10.1038/nature12345", self.out, {}, max_files=10)
        self.assertEqual(len(saved), 2)  # gate page dropped, 3 candidates -> 2 files
        self.assertTrue(any(f.endswith(".xlsx") for f in saved))
        self.assertTrue(any(f.endswith(".pdf") for f in saved))
        self.assertTrue(all("_SI" in f for f in saved))
        self.assertFalse(any("MOESM1" in f for f in saved))

    def test_no_article_url_returns_empty(self):
        with patch("scansci_pdf.supplementary._article_url", return_value=None):
            self.assertEqual(fetch_supplementary("10.1/none", self.out, {}), [])


class BatchWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def _cfg(self, tmp_name):
        return {
            "cache_dir": str(Path(tmp_name) / "cache"),
            "download_si": True,
            "lane_oa_enrich": False,
        }

    def test_download_si_off_by_default_no_calls(self):
        entries = [QueueEntry(identifier="10.1234/x", channel="oa", oa_url="https://a.org/x.pdf")]
        results = [{"success": True, "doi": "10.1234/x", "file": str(self.out / "x.pdf")}]
        with patch.object(pipeline, "fetch_supplementary",
                          side_effect=AssertionError("must not run when switch is off")):
            pipeline._fetch_si_for_results(results, self.out, {"download_si": False})
        self.assertFalse((self.out / "si_manifest.json").exists())

    def test_manifest_written_for_successful_results(self):
        entries = [QueueEntry(identifier="10.1234/x", channel="oa", oa_url="https://a.org/x.pdf")]
        results = [
            {"success": True, "doi": "10.1234/x", "file": str(self.out / "x.pdf")},
            {"success": False, "doi": "10.1234/y"},
        ]
        with patch.object(pipeline, "fetch_supplementary",
                          return_value=[str(self.out / "x_SI1.pdf")]):
            pipeline._fetch_si_for_results(results, self.out, self._cfg(self.tmp.name))
        manifest = json.loads((self.out / "si_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest, {"10.1234/x": [str(self.out / "x_SI1.pdf")]})

    def test_si_failure_never_raises(self):
        results = [{"success": True, "doi": "10.1234/x", "file": str(self.out / "x.pdf")}]
        with patch.object(pipeline, "fetch_supplementary", side_effect=RuntimeError("boom")):
            pipeline._fetch_si_for_results(results, self.out, self._cfg(self.tmp.name))
        self.assertFalse((self.out / "si_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
