"""清单解析：各种 arXiv 写法（裸编号/前缀/URL/旧式）都必须被识别。"""

import unittest

from scansci_pdf.pipeline import QueueEntry, extract_identifier, parse_queue


class ExtractIdentifierArxivTests(unittest.TestCase):
    def test_bare_new_style(self):
        self.assertEqual(extract_identifier("2401.12345"), "2401.12345")

    def test_prefixed_new_style(self):
        self.assertEqual(extract_identifier("arXiv:2401.12345"), "2401.12345")

    def test_abs_url(self):
        self.assertEqual(extract_identifier("https://arxiv.org/abs/2401.12345"), "2401.12345")

    def test_pdf_url(self):
        self.assertEqual(extract_identifier("https://arxiv.org/pdf/2401.12345"), "2401.12345")

    def test_bare_old_style_regression(self):
        """回归：裸旧式 arXiv（quant-ph/9901001）曾被守卫条件拒绝。"""
        self.assertEqual(extract_identifier("quant-ph/9901001"), "quant-ph/9901001")

    def test_prefixed_old_style(self):
        self.assertEqual(extract_identifier("arXiv:quant-ph/9901001"), "quant-ph/9901001")

    def test_doi_still_wins(self):
        self.assertEqual(extract_identifier("10.1016/j.x"), "10.1016/j.x")


class ParseQueueArxivTests(unittest.TestCase):
    def test_mixed_list_all_resolved(self):
        q = "\n".join([
            "2401.12345",
            "arXiv:quant-ph/9901001",
            "https://arxiv.org/abs/2401.12345",
            "quant-ph/9901001",
        ])
        entries = parse_queue(q)
        self.assertFalse(any(e.unresolved for e in entries),
                         "mixed arXiv formats must all resolve")
        self.assertTrue(all(e.identifier for e in entries))


if __name__ == "__main__":
    unittest.main()
