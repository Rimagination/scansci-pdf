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


class QueueContractTests(unittest.TestCase):
    """队列契约：无表头三列 TSV（identifier/channel/oa_url）完整生效。"""

    def test_single_row_headerless_tsv(self):
        """回归：单行无表头队列曾被 DictReader 当表头消费而归零。"""
        entries = parse_queue("10.3390/agronomy11112097\toa\thttps://www.mdpi.com/x.pdf")
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e.identifier, "10.3390/agronomy11112097")
        self.assertEqual(e.channel, "oa")
        self.assertEqual(e.oa_url, "https://www.mdpi.com/x.pdf")
        self.assertFalse(e.unresolved)

    def test_multi_row_headerless_respects_columns(self):
        lines = "\n".join([
            "10.3390/a1\toa\thttps://mdpi.com/a.pdf",
            "10.1016/b1\telsevier\t",
            "10.1007/c1\tinstitution\t",
        ])
        entries = parse_queue(lines)
        self.assertEqual(len(entries), 3)
        by_id = {e.identifier: e for e in entries}
        self.assertEqual(by_id["10.3390/a1"].channel, "oa")
        self.assertEqual(by_id["10.3390/a1"].oa_url, "https://mdpi.com/a.pdf")
        self.assertEqual(by_id["10.1016/b1"].channel, "elsevier")
        self.assertEqual(by_id["10.1007/c1"].channel, "institution")

    def test_header_tsv_still_works(self):
        q = "identifier\tchannel\toa_url\n10.3390/a1\toa\thttps://mdpi.com/a.pdf"
        entries = parse_queue(q)
        # parse_queue 保留无法解析的行（表头行变 unresolved 条目，设计行为）
        resolved = [e for e in entries if not e.unresolved]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].channel, "oa")
        self.assertEqual(resolved[0].oa_url, "https://mdpi.com/a.pdf")

    def test_plain_doi_lines_still_work(self):
        entries = parse_queue("10.1016/j.x\n10.1038/y")
        self.assertTrue(all(e.identifier for e in entries))
        self.assertFalse(any(e.unresolved for e in entries))
