"""Author resolution must prefer the authoritative OpenAlex profile.

Regression for the "Shilong Piao" case: OpenAlex returns a 1-work duplicate
named exactly like the query and the real 999-work profile that carries a
middle initial. Ranking inside exact matches alone picked the duplicate.

Run: python -m pytest tests/test_search.py
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scansci_pdf import search


AUTHORITATIVE = {
    "id": "https://openalex.org/A5000033522",
    "display_name": "Shilong L. Piao",
    "works_count": 999,
    "cited_by_count": 125224,
}
DUPLICATE = {
    "id": "https://openalex.org/A5072538516",
    "display_name": "Shilong Piao",
    "works_count": 1,
    "cited_by_count": 0,
}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResponse(self._payload, self.status_code)


def _patch_authors(results, status_code=200):
    fake = _FakeSession({"results": results}, status_code)
    return patch.object(search, "_plain_session", return_value=fake)


class NameCompatibilityTests(unittest.TestCase):
    def test_middle_initial_is_tolerated(self):
        self.assertTrue(search._name_compatible("Shilong Piao", "Shilong L. Piao"))

    def test_tolerance_is_symmetric(self):
        self.assertTrue(search._name_compatible("Shilong L. Piao", "Shilong Piao"))

    def test_reversed_order_is_tolerated(self):
        self.assertTrue(search._name_compatible("Fang Jingyun", "Jingyun Fang"))

    def test_initials_do_not_expand_to_spelled_out_names(self):
        self.assertFalse(search._name_compatible("J. Smith", "John Smith"))

    def test_different_people_are_not_compatible(self):
        self.assertFalse(search._name_compatible("Shilong Piao", "Wei Zhang"))

    def test_missing_suffix_is_tolerated(self):
        self.assertTrue(search._name_compatible("John Smith", "John Smith Jr."))

    def test_different_suffixes_are_not_compatible(self):
        self.assertFalse(search._name_compatible("John Smith Jr.", "John Smith Sr."))
        self.assertFalse(search._name_compatible("John Smith III", "John Smith IV"))

    def test_compact_initials_are_split(self):
        self.assertTrue(search._name_compatible("Shilong Piao", "Shilong L.Piao"))
        self.assertTrue(search._name_compatible("J.R.R. Tolkien", "J. R. R. Tolkien"))


class ResolveAuthorTests(unittest.TestCase):
    def test_authoritative_profile_beats_exact_name_duplicate(self):
        with _patch_authors([AUTHORITATIVE, DUPLICATE]):
            match = search._resolve_author("Shilong Piao")

        self.assertIsNotNone(match)
        self.assertEqual(match.id, "A5000033522")
        self.assertEqual(match.works_count, 999)
        # The chosen profile is not an exact string match; callers can see that.
        self.assertFalse(match.exact)

    def test_duplicate_is_reported_as_an_alternative(self):
        with _patch_authors([AUTHORITATIVE, DUPLICATE]):
            match = search._resolve_author("Shilong Piao")

        alt_ids = [a["id"] for a in match.alternatives]
        self.assertIn("A5072538516", alt_ids)
        self.assertNotIn("A5000033522", alt_ids)

    def test_exact_name_match_is_flagged_exact(self):
        with _patch_authors([DUPLICATE]):
            match = search._resolve_author("Shilong Piao")

        self.assertEqual(match.id, "A5072538516")
        self.assertTrue(match.exact)
        self.assertEqual(match.alternatives, ())

    def test_alternatives_are_capped_at_three(self):
        others = [
            dict(DUPLICATE, id=f"https://openalex.org/A{i}", display_name=f"Shilong Piao {i}")
            for i in range(6)
        ]
        with _patch_authors([AUTHORITATIVE, *others]):
            match = search._resolve_author("Shilong Piao")

        self.assertEqual(match.id, "A5000033522")
        self.assertFalse(match.fallback)
        self.assertEqual(len(match.alternatives), 3)

    def test_falls_back_to_all_candidates_when_none_is_compatible(self):
        """Pre-existing behaviour: differing names still resolve to the largest
        profile, which keeps "first name spelled differently" usable."""
        other = dict(DUPLICATE, display_name="Wei Zhang", cited_by_count=5)
        with _patch_authors([other]):
            match = search._resolve_author("Shilong Piao")

        self.assertEqual(match.id, "A5072538516")
        self.assertFalse(match.exact)
        self.assertTrue(match.fallback)
        self.assertEqual(match.alternatives, ())

    def test_http_error_returns_none(self):
        with _patch_authors([AUTHORITATIVE], status_code=503):
            self.assertIsNone(search._resolve_author("Shilong Piao"))

    def test_empty_results_return_none(self):
        with _patch_authors([]):
            self.assertIsNone(search._resolve_author("Shilong Piao"))

    def test_network_exception_returns_none(self):
        with patch.object(search, "_plain_session", side_effect=OSError("offline")):
            self.assertIsNone(search._resolve_author("Shilong Piao"))

    def test_lookup_author_id_stays_a_four_tuple(self):
        with _patch_authors([AUTHORITATIVE, DUPLICATE]):
            author_id, display, works, cited = search._lookup_author_id("Shilong Piao")

        self.assertEqual(author_id, "A5000033522")
        self.assertEqual(display, "Shilong L. Piao")
        self.assertEqual((works, cited), (999, 125224))


class SearchPapersMetadataTests(unittest.TestCase):
    def test_author_match_metadata_carries_exact_flag_and_alternatives(self):
        match = search.AuthorMatch(
            id="A5000033522",
            display_name="Shilong L. Piao",
            works_count=999,
            cited_by_count=125224,
            exact=False,
            alternatives=({"id": "A5072538516", "display_name": "Shilong Piao"},),
        )
        with patch.object(search, "_resolve_author", return_value=match), \
             patch.object(search, "_search_openalex_by_author", return_value=[{"doi": "10.1/x"}]):
            results = search.search_papers(author="Shilong Piao", sort="cited_by_count")

        meta = results[0]["_author_match"]
        self.assertEqual(meta["id"], "A5000033522")
        self.assertEqual(meta["query"], "Shilong Piao")
        self.assertFalse(meta["exact"])
        self.assertFalse(meta["fallback"])
        self.assertEqual(meta["alternatives"][0]["id"], "A5072538516")

    def test_unresolved_author_returns_no_results(self):
        with patch.object(search, "_resolve_author", return_value=None):
            self.assertEqual(search.search_papers(author="Nobody Here"), [])


if __name__ == "__main__":
    unittest.main()
