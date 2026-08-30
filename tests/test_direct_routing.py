"""Tests for per-domain direct routing in select_proxy_for_url.

sci-hub.ru rate-limits by egress IP: every proxy user shares one exit, so
proxied requests got a standing verification wall while direct connections
from the same machine were clean (measured 2026-08-30). Those hosts route
direct by default; Tor keeps top priority; direct_domains extends the set.
"""

import unittest
from unittest.mock import patch

from scansci_pdf.network import select_proxy_for_url

PROXY = "http://127.0.0.1:7890"


class DirectRoutingTests(unittest.TestCase):
    def test_scihub_ru_routes_direct_by_default(self):
        self.assertIsNone(
            select_proxy_for_url("https://sci-hub.ru/10.1093/nar/x", {"network_proxy": PROXY})
        )

    def test_other_domains_still_use_proxy(self):
        self.assertEqual(
            select_proxy_for_url("https://academic.oup.com/x", {"network_proxy": PROXY}),
            PROXY,
        )

    def test_subdomains_covered(self):
        self.assertIsNone(
            select_proxy_for_url("https://www.sci-hub.ru/x", {"network_proxy": PROXY})
        )

    def test_direct_domains_config_extends_set(self):
        cfg = {"network_proxy": PROXY, "direct_domains": ["example.org"]}
        self.assertIsNone(select_proxy_for_url("https://example.org/x", cfg))
        self.assertEqual(select_proxy_for_url("https://other.org/x", cfg), PROXY)

    def test_kill_switch_restores_proxy(self):
        cfg = {"network_proxy": PROXY, "scihub_direct": False}
        self.assertEqual(
            select_proxy_for_url("https://sci-hub.ru/x", cfg), PROXY
        )

    def test_tor_still_wins(self):
        cfg = {"network_proxy": PROXY}
        with patch("scansci_pdf.tor.ensure_tor", return_value="socks5://127.0.0.1:9050"):
            self.assertEqual(
                select_proxy_for_url("https://sci-hub.ru/x", cfg, use_tor=True),
                "socks5://127.0.0.1:9050",
            )


if __name__ == "__main__":
    unittest.main()
