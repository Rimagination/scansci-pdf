"""Elsevier key 权限自检与逐篇探测的判定逻辑测试（网络探测全部 mock）。"""

import unittest
from unittest.mock import patch

from scansci_pdf import elsevier_check as ec


def _patch_key_valid(verdict: str, code: int = 200):
    return patch.object(ec, "_probe_key_valid", return_value=(code, verdict))


def _patch_diag(text: str = ""):
    """403 GET 诊断（view=META）打桩——默认空串=未命中特定 message。"""
    return patch.object(ec, "_error_status_text_probe", return_value=text)


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


class KeyValidProbeTests(unittest.TestCase):
    def test_valid_key(self):
        with patch.object(ec.requests, "get") as get:
            get.return_value.status_code = 200
            code, verdict = ec._probe_key_valid("K", None)
        self.assertEqual(verdict, ec.KEY_VALID)

    def test_quota_means_authenticated(self):
        with patch.object(ec.requests, "get") as get:
            get.return_value.status_code = 429
            code, verdict = ec._probe_key_valid("K", None)
        self.assertEqual(verdict, ec.KEY_VALID)

    def test_invalid_key(self):
        with patch.object(ec.requests, "get") as get:
            get.return_value.status_code = 401
            code, verdict = ec._probe_key_valid("BAD", None)
        self.assertEqual(verdict, ec.KEY_INVALID)

    def test_network_error(self):
        with patch.object(ec.requests, "get", side_effect=TimeoutError):
            code, verdict = ec._probe_key_valid("K", None)
        self.assertEqual(verdict, f"{ec.NETWORK}(TimeoutError)")
        self.assertEqual(code, 0)


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
        with patch.object(ec, "_head_probe", side_effect=fake), _patch_key_valid(ec.KEY_VALID):
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "广覆盖机构 key")
        self.assertIn("API", advice)
        self.assertEqual(len(rows), 5)  # key认证 + 无key基线 + OA对照 + 大刊双路由

    def test_key_valid_but_no_subscription(self):
        def fake(doi, key, px, *, http_accept=True):
            if http_accept is False:
                return 200, ec.ENTITLED  # OA 对照
            if not key:
                return 406, ec.NO_KEY
            return 403, ec.NOT_ENTITLED
        with patch.object(ec, "_head_probe", side_effect=fake), \
                _patch_key_valid(ec.KEY_VALID), _patch_diag():
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "key 有效但无该刊订阅")
        self.assertIn("重新注册", advice)

    def test_network_unreachable(self):
        def fake(doi, key, px, *, http_accept=True):
            return 0, f"{ec.NETWORK}(Timeout)"
        with patch.object(ec, "_head_probe", side_effect=fake), _patch_key_valid(f"{ec.NETWORK}(Timeout)", 0):
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
        with patch.object(ec, "_head_probe", side_effect=fake), \
                _patch_key_valid(ec.KEY_VALID), _patch_diag():
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "key 有效但无该刊订阅")

    def test_valid_key_without_subscription_is_not_invalid(self):
        """#56 回归：key 本身有效（serial/title 200）但大刊样本 403、OA 对照
        也失手时，画像必须是"无该刊订阅"而不是"无效 key"。"""
        def fake(doi, key, px, *, http_accept=True):
            if http_accept is False:
                return 0, f"{ec.NETWORK}(Timeout)"  # OA 对照失手
            if not key:
                return 403, ec.NOT_ENTITLED  # 基线也非 406
            return 403, ec.NOT_ENTITLED  # 大刊无权益
        with patch.object(ec, "_head_probe", side_effect=fake), \
                _patch_key_valid(ec.KEY_VALID), _patch_diag():
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "key 有效但无该刊订阅")

    def test_truly_invalid_key(self):
        def fake(doi, key, px, *, http_accept=True):
            if http_accept is False:
                return 200, ec.ENTITLED
            if not key:
                return 406, ec.NO_KEY
            return 403, ec.NOT_ENTITLED
        with patch.object(ec, "_head_probe", side_effect=fake), _patch_key_valid(ec.KEY_INVALID, 401):
            rows, profile, advice = ec.check_key_profile(self.cfg)
        self.assertEqual(profile, "无效 key")
        self.assertIn("elsevier-setup", advice)


REQUESTOR_TEXT = "Requestor configuration settings insufficient for access to this resource"


class RequestorConfig403Tests(unittest.TestCase):
    """403 'Requestor configuration settings insufficient'：点名 message 并按出口路由区分两种成因。"""

    def test_status_text_extracts_from_namespaced_xml(self):
        body = ('<?xml version="1.0"?><service-error xmlns="https://api.elsevier.com">'
                '<status><statusCode>AUTHENTICATION_ERROR</statusCode>'
                f'<statusText>{REQUESTOR_TEXT}</statusText></status></service-error>')
        self.assertEqual(ec._status_text(body), REQUESTOR_TEXT)

    def test_status_text_malformed_body(self):
        self.assertEqual(ec._status_text("not xml"), "")
        self.assertEqual(ec._status_text("<other><a>1</a></other>"), "")

    def _dual_403(self, cfg, diag_text):
        def fake(doi, key, px, *, http_accept=True):
            return 403, ec.NOT_ENTITLED
        with patch.object(ec, "_head_probe", side_effect=fake), \
                patch.object(ec.time, "sleep"), _patch_diag(diag_text):
            return ec.probe_dual_route("10.1016/j.geoderma.2023.116365", "K", cfg)

    def test_direct_route_403_names_campus_egress_fix(self):
        with patch.object(ec, "configured_proxy", return_value=None):
            row = self._dual_403({}, REQUESTOR_TEXT)  # 未走校园出口
        self.assertEqual(row["verdict"], ec.NOT_ENTITLED)
        self.assertIn("network_proxy", row["route_advice"])
        self.assertIn("校园出口", row["route_advice"])
        self.assertIn("statusText", row["note"])

    def test_proxy_route_403_names_admin_and_insttoken(self):
        cfg = {"network_proxy": "http://127.0.0.1:7890"}
        row = self._dual_403(cfg, REQUESTOR_TEXT)  # 已走配置出口仍 403
        self.assertIn("管理员", row["route_advice"])
        self.assertIn("insttoken", row["route_advice"])

    def test_other_403_keeps_base_advice(self):
        row = self._dual_403({}, "")  # 诊断未命中该 message
        self.assertEqual(row["route_advice"], ec.ROUTE_ADVICE[ec.NOT_ENTITLED])
        self.assertEqual(row["note"], "")

    def test_profile_refines_advice_on_requestor_config(self):
        cfg = {"elsevier_api_key": "K", "network_proxy": "http://127.0.0.1:7890"}
        def fake(doi, key, px, *, http_accept=True):
            if http_accept is False:
                return 200, ec.ENTITLED
            if not key:
                return 406, ec.NO_KEY
            return 403, ec.NOT_ENTITLED
        with patch.object(ec, "_head_probe", side_effect=fake), \
                _patch_key_valid(ec.KEY_VALID), _patch_diag(REQUESTOR_TEXT):
            rows, profile, advice = ec.check_key_profile(cfg)
        self.assertEqual(profile, "key 有效但无该刊订阅")
        self.assertIn("管理员", advice)
        self.assertIn("insttoken", advice)


if __name__ == "__main__":
    unittest.main()
