"""Optica (formerly OSA, DOI prefix 10.1364) publisher-batch profile (#59).

Before #59 there was no entry in PUBLISHER_PROFILES, so `publisher-batch
--publisher optica` and prefix-based inference fell through to the generic
channel with no Optica-specific PDF routes.
"""

import unittest

from scansci_pdf.institutional.publisher_profiles import (
    _PROFILE_ALIASES,
    get_publisher_profile,
    infer_publisher_profile,
)


class OpticaProfileTests(unittest.TestCase):
    def test_resolves_by_key_and_aliases(self):
        for name in ("optica", "osa", "opg"):
            self.assertEqual(get_publisher_profile(name).name, "Optica")

    def test_inferred_from_doi_prefix(self):
        profile = infer_publisher_profile("10.1364/OL.47.013297")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "Optica")

    def test_pdf_urls_use_viewmedia_doi_shape(self):
        profile = get_publisher_profile("optica")
        urls = profile.pdf_urls("10.1364/OL.47.013297")
        self.assertTrue(urls)
        for url in urls:
            self.assertIn("opg.optica.org/viewmedia.cfm", url)
            self.assertIn("uri=10.1364", url)
        self.assertIn("uri=10.1364/OL.47.013297", urls[0])       # raw
        self.assertIn("uri=10.1364%2FOL.47.013297", urls[-1])    # quoted variant

    def test_urls_are_doi_quoted_safe(self):
        profile = get_publisher_profile("optica")
        for url in profile.pdf_urls("10.1364/OL.47.013297"):
            self.assertNotIn("{", url)

    def test_alias_table_contains_sage_carsi_unaffected(self):
        # sanity: the alias map still resolves other publishers
        self.assertIn("wiley", _PROFILE_ALIASES)


if __name__ == "__main__":
    unittest.main()
