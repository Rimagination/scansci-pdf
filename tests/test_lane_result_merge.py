"""Lane results must be summarised per paper, not per attempt.

The lane scheduler emits one row per attempt, so a paper that misses the fast
lane and lands in the grey lane leaves a failed row *and* a success row. The
batch summary counted rows, so a fully successful batch reported
``succeeded: 20, failed: 4``.
"""

from __future__ import annotations

import unittest

from scansci_pdf.server import _merge_lane_results


def _ok(doi: str) -> dict:
    return {"doi": doi, "success": True, "source": "oa_url", "file": f"{doi}.pdf"}


def _fail(doi: str, why: str = "fast lane failed") -> dict:
    return {"doi": doi, "success": False, "error": why}


class MergeLaneResultsTests(unittest.TestCase):
    def test_retry_success_replaces_the_failed_attempt(self):
        raw = [_fail("10.1/a"), _ok("10.1/a"), _ok("10.1/b")]
        rows = _merge_lane_results(raw)

        self.assertEqual(len(rows), 2)
        merged = {r["doi"]: r for r in rows}
        self.assertTrue(merged["10.1/a"]["success"])

    def test_no_failed_rows_survive_a_retry(self):
        rows = _merge_lane_results([_fail("10.1/a"), _ok("10.1/a")])
        self.assertEqual([r for r in rows if not r.get("success")], [])

    def test_genuine_failure_is_kept_once(self):
        rows = _merge_lane_results([_fail("10.1/x"), _fail("10.1/x", "grey also failed")])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["success"])

    def test_later_failure_reason_wins(self):
        rows = _merge_lane_results([
            _fail("10.1/x", "fast lane failed"),
            _fail("10.1/x", "cloudflare_blocked"),
        ])
        self.assertEqual(rows[0]["error"], "cloudflare_blocked")

    def test_earlier_source_failures_are_preserved(self):
        rows = _merge_lane_results([
            {
                "doi": "10.1/x", "success": False, "error": "fast lane failed",
                "source_failures": [{"source": "OA", "reason": "miss"}],
            },
            {"doi": "10.1/x", "success": False, "error": "grey failed"},
        ])
        self.assertEqual(rows[0]["error"], "grey failed")
        self.assertEqual(rows[0]["source_failures"][0]["source"], "OA")

    def test_success_wins_even_when_it_came_first(self):
        rows = _merge_lane_results([_ok("10.1/a"), _fail("10.1/a")])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["success"])

    def test_doi_and_identifier_rows_for_one_paper_are_merged(self):
        raw = [
            {"identifier": "10.1/a", "success": False, "error": "fast lane"},
            {"doi": "10.1/a", "identifier": "10.1/a", "success": True},
        ]
        rows = _merge_lane_results(raw)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["success"])

    def test_keys_are_case_and_whitespace_insensitive(self):
        raw = [_fail(" 10.1/ABC "), _ok("10.1/abc")]
        self.assertEqual(len(_merge_lane_results(raw)), 1)

    def test_doi_url_and_bare_doi_are_merged(self):
        raw = [_fail("https://doi.org/10.1/ABC"), _ok("10.1/abc")]
        self.assertEqual(len(_merge_lane_results(raw)), 1)

    def test_arxiv_prefixes_are_merged(self):
        raw = [_fail("arXiv:2401.12345"), _ok("2401.12345")]
        self.assertEqual(len(_merge_lane_results(raw)), 1)

    def test_unrecognized_rows_are_never_collapsed(self):
        raw = [
            {"doi": "", "success": False, "error": "unrecognized identifier"},
            {"doi": "", "success": False, "error": "unrecognized identifier"},
        ]
        self.assertEqual(len(_merge_lane_results(raw)), 2)

    def test_first_appearance_order_is_preserved(self):
        raw = [_fail("10.1/b"), _fail("10.1/a"), _ok("10.1/b")]
        self.assertEqual([r["doi"] for r in _merge_lane_results(raw)], ["10.1/b", "10.1/a"])

    def test_empty_input(self):
        self.assertEqual(_merge_lane_results([]), [])


if __name__ == "__main__":
    unittest.main()
