"""Elsevier key 权限自检与逐篇探测的判定逻辑测试（网络探测全部 mock）。"""

import unittest
from unittest.mock import patch

from scansci_pdf import elsevier_check as ec


class NormalizeTests(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(ec.normalize_status(200), "ENTITLED")
        self.assertEqual(ec.normalize_status(403), "NOT_ENTITLED")
        self.assertEqual(ec.normalize_status(404), "NOT_FOUND")
        self.assertEqual(ec.normalize_status(429), "QUOTA")
        self.assertEqual(ec.normalize_status(500), "NETWORK")
        self.assertEqual(ec.normalize_status(406), "NO_KEY")


class CombineTests(unittest.TestCase):
    def test_any_entitled_wins(self):
        v, adv = ec._combine(ec.NOT_ENTITLED, ec.ENTITLED)
        self.assertEqual(v, ec.ENTITLED)
        self.assertIn("API", adv)

    def test_both_not_entitled(self):
        v, adv = ec._combine(ec.NOT_ENTITLED, ec.NOT_ENTITLED)
        self.assertEqual(v, ec.NOT_ENTITLED)
        self.assertIn("机构", adv)

    def test_not_found(self):
        v, adv = ec._combine(ec.NOT_FOUND, ec.NOT_FOUND)
        self.assertEqual(v, ec.NOT_FOUND)
        self.assertIn("核对", adv)

    def test_quota(self):
        v, adv = ec._combine(ec.QUOTA, ec.NOT_ENTITLED)
        self.assertEqual(v, ec.QUOTA)
        self.assertIn("节流", adv)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"elsevier_api_key": "K", "network_proxy": "http://127.0.0.1:7890"}

    def test_broad_institutional_key(self):
        def fake(doi, key, px, *, http_accept=True):
            if http_accept is False:
                return 200, ec.ENTITLED  # OA 对照
            if not key:
                return 406, ec.NO_KEY
            return 200, ec.ENTITLED
        with patch.object(ec, "_head_probe", side_effect=fake):
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "广覆盖机构 key")
        self.assertIn("API", advice)
        self.assertEqual(len(rows), 4)  # 无key基线 + OA对照 + 大刊双路由

    def test_key_valid_but_no_subscription(self):
        def fake(doi, key, px, *, http_accept=True):
            if http_accept is False:
                return 200, ec.ENTITLED  # OA 对照
            if not key:
                return 406, ec.NO_KEY
            return 403, ec.NOT_ENTITLED
        with patch.object(ec, "_head_probe", side_effect=fake):
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "key 有效但无该刊订阅")
        self.assertIn("重新注册", advice)

    def test_network_unreachable(self):
        def fake(doi, key, px, *, http_accept=True):
            return 0, f"{ec.NETWORK}(Timeout)"
        with patch.object(ec, "_head_probe", side_effect=fake):
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "网络/端点不可达")

    def test_oa_control_verifies_endpoint(self):
        """OA 对照可得 + 大刊 403 = 端点工作、key 无订阅（不是网络问题）。"""
        def fake(doi, key, px, *, http_accept=True):
            if http_accept is False:
                return 200, ec.ENTITLED
            if not key:
                return 406, ec.NO_KEY
            return 403, ec.NOT_ENTITLED
        with patch.object(ec, "_head_probe", side_effect=fake):
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "key 有效但无该刊订阅")


if __name__ == "__main__":
    unittest.main()
