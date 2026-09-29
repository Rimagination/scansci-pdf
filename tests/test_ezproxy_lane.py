"""EZProxy lane URL shaping and session validation (issues #62).

Hostname-rewriting deployments (HKUST) never proxy through a
``login?url={url}`` prefix — they need ``{host_dashed}`` rewriting, and both
session validation and login detection used to misread them.
"""

import json
import unittest
from unittest.mock import Mock, patch

from scansci_pdf.sources import ezproxy

PREFIX_CONFIG = {"ezproxy_login_url": "https://ezproxy.lib.foo.edu/login?url={url}"}
REWRITE_CONFIG = {"ezproxy_login_url": "https://{host_dashed}.lib.ezproxy.hkust.edu.hk"}


class MakeEzproxyUrlTests(unittest.TestCase):
    def test_prefix_template_substitutes_target(self):
        self.assertEqual(
            ezproxy._make_ezproxy_url("https://www.nature.com/articles/x", PREFIX_CONFIG),
            "https://ezproxy.lib.foo.edu/login?url=https://www.nature.com/articles/x",
        )

    def test_host_dashed_template_rewrites_host(self):
        self.assertEqual(
            ezproxy._make_ezproxy_url(
                "https://onlinelibrary.wiley.com/doi/10.1002/anie.202526147", REWRITE_CONFIG),
            "https://onlinelibrary-wiley-com.lib.ezproxy.hkust.edu.hk/doi/10.1002/anie.202526147",
        )

    def test_host_dashed_preserves_query(self):
        self.assertEqual(
            ezproxy._make_ezproxy_url(
                "https://pubs.acs.org/doi/pdf/10.1021/x?download=true", REWRITE_CONFIG),
            "https://pubs-acs-org.lib.ezproxy.hkust.edu.hk/doi/pdf/10.1021/x?download=true",
        )

    def test_unknown_template_yields_empty(self):
        self.assertEqual(ezproxy._make_ezproxy_url("https://a.b/c", {}), "")
        self.assertEqual(ezproxy._make_ezproxy_url("https://a.b/c", {"ezproxy_login_url": "https://p.edu/"}), "")


class EzproxyOriginTests(unittest.TestCase):
    def test_prefix_origin(self):
        self.assertEqual(ezproxy._ezproxy_origin(PREFIX_CONFIG), "https://ezproxy.lib.foo.edu")

    def test_rewrite_origin_strips_label(self):
        self.assertEqual(
            ezproxy._ezproxy_origin(REWRITE_CONFIG),
            "https://lib.ezproxy.hkust.edu.hk",
        )


class LooksLikeLoginUrlTests(unittest.TestCase):
    def test_login_paths_detected(self):
        self.assertTrue(ezproxy._looks_like_login_url("https://p.edu/login?url=https://a.b"))
        self.assertTrue(ezproxy._looks_like_login_url("https://idp.edu/wayf?x=1"))
        self.assertTrue(ezproxy._looks_like_login_url("https://p.edu/Shibboleth.sso/Login"))

    def test_proxied_host_is_not_login(self):
        # Rewritten proxy URLs keep the marker token in the HOST for every
        # authenticated page — that must not read as a login challenge (#62).
        self.assertFalse(ezproxy._looks_like_login_url(
            "https://onlinelibrary-wiley-com.libproxy.berkeley.edu/doi/pdf/10.1002/x"))
        self.assertFalse(ezproxy._looks_like_login_url(
            "https://www-sciencedirect-com.lib.ezproxy.hkust.edu.hk/science/article/pii/X"))


def _cookieJar_response(status=200, url="https://lib.ezproxy.hkust.edu.hk/menu", text=""):
    resp = Mock()
    resp.status_code = status
    resp.url = url
    resp.text = text
    return resp


class ValidateSessionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="scansci-ezproxy-test-")

    def _write_cookies(self, config, cookies):
        from pathlib import Path
        cache = Path(config["cache_dir"])
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "ezproxy_cookies.json").write_text(json.dumps(cookies), encoding="utf-8")

    def test_rejects_jar_without_proxy_cookie(self):
        # The HKUST false-positive login saved 17 publisher cookies and zero
        # proxy cookies — such a jar must fail validation outright (#62).
        cfg = {**REWRITE_CONFIG, "cache_dir": self._tmp}
        self._write_cookies(cfg, [{"name": "s", "value": "1", "domain": ".sciencedirect.com", "path": "/"}])
        self.assertFalse(ezproxy._validate_ezproxy_session(cfg))

    def test_accepts_live_session_on_rewrite_deployment(self):
        cfg = {**REWRITE_CONFIG, "cache_dir": self._tmp}
        self._write_cookies(cfg, [{"name": "ezproxy", "value": "1", "domain": ".lib.ezproxy.hkust.edu.hk", "path": "/"}])
        with patch.object(ezproxy.requests.Session, "get",
                          return_value=_cookieJar_response(200, "https://lib.ezproxy.hkust.edu.hk/menu")):
            self.assertTrue(ezproxy._validate_ezproxy_session(cfg))

    def test_rejects_login_redirect(self):
        cfg = {**REWRITE_CONFIG, "cache_dir": self._tmp}
        self._write_cookies(cfg, [{"name": "ezproxy", "value": "1", "domain": ".lib.ezproxy.hkust.edu.hk", "path": "/"}])
        with patch.object(ezproxy.requests.Session, "get",
                          return_value=_cookieJar_response(200, "https://lib.ezproxy.hkust.edu.hk/login")):
            self.assertFalse(ezproxy._validate_ezproxy_session(cfg))

    def test_rejects_shibboleth_challenge_page(self):
        cfg = {**REWRITE_CONFIG, "cache_dir": self._tmp}
        self._write_cookies(cfg, [{"name": "ezproxy", "value": "1", "domain": ".lib.ezproxy.hkust.edu.hk", "path": "/"}])
        with patch.object(ezproxy.requests.Session, "get",
                          return_value=_cookieJar_response(
                              200, "https://lib.ezproxy.hkust.edu.hk/login",
                              "<title>Shibboleth Authentication Request</title>")):
            self.assertFalse(ezproxy._validate_ezproxy_session(cfg))

    def test_prefix_deployment_validates_via_origin(self):
        cfg = {**PREFIX_CONFIG, "cache_dir": self._tmp}
        self._write_cookies(cfg, [{"name": "ezproxy", "value": "1", "domain": ".ezproxy.lib.foo.edu", "path": "/"}])
        with patch.object(ezproxy.requests.Session, "get",
                          return_value=_cookieJar_response(200, "https://ezproxy.lib.foo.edu/menu")) as get:
            self.assertTrue(ezproxy._validate_ezproxy_session(cfg))
        self.assertEqual(get.call_args[0][0], "https://ezproxy.lib.foo.edu/login")


class ProxyDomainTests(unittest.TestCase):
    def test_markers(self):
        self.assertTrue(ezproxy._is_proxy_domain(".lib.ezproxy.hkust.edu.hk"))
        self.assertTrue(ezproxy._is_proxy_domain("onlinelibrary-wiley-com.libproxy.berkeley.edu"))
        self.assertFalse(ezproxy._is_proxy_domain(".sciencedirect.com"))
        self.assertFalse(ezproxy._is_proxy_domain(""))


if __name__ == "__main__":
    unittest.main()
