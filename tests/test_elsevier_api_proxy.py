"""Elsevier API 下载链走显式代理的回归测试（网络请求全部 mock）。

背景（用户实测，2026-09）：权限探测（elsevier_check 双路由）认 network_proxy，
但快车道/API 下载器（sources/elsevier_api）忽略所有代理设置——用户挂了
校园网代理仍全员 403 NOT_ENTITLED。修复后整条链统一走
SCANSCI_PDF_PROXY / network_proxy（HTTP_PROXY/HTTPS_PROXY 项目级忽略）。
"""

import os
import unittest
from unittest.mock import patch

from scansci_pdf import elsevier_check as ec
from scansci_pdf.sources import elsevier_api as ea

CAMPUS_PROXY = "http://127.0.0.1:7890"


class _FakeResp:
    def __init__(self, status_code=403, text="", content=b""):
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = {}


class _FakeSession:
    """记录 trust_env/proxies 配置的替身 Session。"""

    instances: list["_FakeSession"] = []

    def __init__(self):
        self.trust_env = True
        self.proxies: dict = {}
        self.headers: dict = {}
        self.calls: list[dict] = []
        _FakeSession.instances.append(self)

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeResp(403)


class ResolveProxiesTests(unittest.TestCase):
    def test_none_without_any_setting(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ea._resolve_proxies({}))
            self.assertIsNone(ea._resolve_proxies(None))

    def test_config_proxy(self):
        self.assertEqual(
            ea._resolve_proxies({"network_proxy": CAMPUS_PROXY}),
            {"http": CAMPUS_PROXY, "https": CAMPUS_PROXY},
        )

    def test_env_overrides_config(self):
        with patch.dict(os.environ, {"SCANSCI_PDF_PROXY": "socks5://127.0.0.1:1080"}):
            self.assertEqual(
                ea._resolve_proxies({"network_proxy": CAMPUS_PROXY}),
                {"http": "socks5://127.0.0.1:1080", "https": "socks5://127.0.0.1:1080"},
            )


class ApiRequestProxyTests(unittest.TestCase):
    def test_proxies_applied_to_session(self):
        _FakeSession.instances = []
        with patch.object(ea.requests, "Session", _FakeSession):
            resp = ea._api_request("https://api.elsevier.com/content/x", {},
                                   proxies={"http": CAMPUS_PROXY, "https": CAMPUS_PROXY})
        self.assertEqual(resp.status_code, 403)
        for s in _FakeSession.instances:
            self.assertEqual(s.proxies["http"], CAMPUS_PROXY)

    def test_direct_when_no_proxies(self):
        _FakeSession.instances = []
        with patch.object(ea.requests, "Session", _FakeSession):
            ea._api_request("https://api.elsevier.com/content/x", {})
        for s in _FakeSession.instances:
            self.assertEqual(s.proxies, {})


class FetchPdfProxyTests(unittest.TestCase):
    """fetch_pdf 全链（XML→attachment→direct fallback）都带同一代理。"""

    def test_chain_uses_config_proxy(self):
        _FakeSession.instances = []
        with patch.object(ea.requests, "Session", _FakeSession):
            out = ea.fetch_pdf("10.1016/j.aaa.2023.01.001", "KEY",
                               config={"network_proxy": CAMPUS_PROXY})
        self.assertIsNone(out)  # 403 全失败，但请求必须都走了代理
        self.assertGreaterEqual(len(_FakeSession.instances), 2)
        for s in _FakeSession.instances:
            self.assertEqual(s.proxies.get("http"), CAMPUS_PROXY)
            self.assertFalse(s.trust_env)

    def test_chain_uses_env_proxy_without_config(self):
        _FakeSession.instances = []
        with patch.dict(os.environ, {"SCANSCI_PDF_PROXY": CAMPUS_PROXY}), \
             patch.object(ea.requests, "Session", _FakeSession):
            ea.fetch_pdf("10.1016/j.aaa.2023.01.001", "KEY")
        for s in _FakeSession.instances:
            self.assertEqual(s.proxies.get("http"), CAMPUS_PROXY)

    def test_direct_without_any_proxy(self):
        _FakeSession.instances = []
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(ea.requests, "Session", _FakeSession):
                ea.fetch_pdf("10.1016/j.aaa.2023.01.001", "KEY", config={})
        for s in _FakeSession.instances:
            self.assertEqual(s.proxies, {})


class InstitutionalMirrorTests(unittest.TestCase):
    def test_fetch_pdf_applies_config_proxy(self):
        from scansci_pdf.institutional.sources import elsevier_api as inst

        _FakeSession.instances = []
        with patch.object(inst.requests, "Session", _FakeSession):
            inst.fetch_pdf("10.1016/j.aaa.2023.01.001", "KEY",
                           config={"network_proxy": CAMPUS_PROXY})
        self.assertTrue(_FakeSession.instances)
        for s in _FakeSession.instances:
            self.assertEqual(s.proxies.get("http"), CAMPUS_PROXY)


class ProbeEnvProxyTests(unittest.TestCase):
    """权限探测与下载器必须同一代理语义（env 优先），否则诊断与实际出口脱节。"""

    def test_probe_dual_route_uses_env_proxy(self):
        captured = []
        with patch.dict(os.environ, {"SCANSCI_PDF_PROXY": CAMPUS_PROXY}), \
             patch.object(ec.time, "sleep"):
            def _capture(doi, key, proxies, **kw):
                captured.append(proxies)
                return 200, ec.ENTITLED
            with patch.object(ec, "_head_probe", side_effect=_capture):
                ec.probe_dual_route("10.1016/j.aaa.2023.01.001", "K", {})
        self.assertEqual(captured[0], {"http": CAMPUS_PROXY, "https": CAMPUS_PROXY})
        self.assertIsNone(captured[1])  # direct 路由不带代理


if __name__ == "__main__":
    unittest.main()
