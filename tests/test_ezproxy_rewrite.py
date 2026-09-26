"""Tests for EZProxy hostname rewriting and session-cookie handling.

Prefix-style templates (``.../login?url={url}``) and hostname-rewriting deployments
(``pubs.acs.org`` -> ``pubs-acs-org.lib.ezproxy.hkust.edu.hk``) both have to work:
on HKUST the prefix forms 302 straight back to the raw publisher and never proxy, so
the rewriting branch is the only one that can fetch institutional full text there.
"""

from scansci_pdf.sources.ezproxy import (
    _has_ezproxy_session_cookie,
    _make_ezproxy_url,
    _rewrite_suffix,
    _sanitize_playwright_cookies,
)

REWRITE = {"ezproxy_login_url": "https://lib.ezproxy.hkust.edu.hk/login"}
PREFIX = {"ezproxy_login_url": "https://libproxy.example.edu/login?url={url}"}


def test_prefix_style_substitutes_target():
    out = _make_ezproxy_url("https://www.sciencedirect.com/article/x", PREFIX)
    assert out == "https://libproxy.example.edu/login?url=https://www.sciencedirect.com/article/x"


def test_rewrite_suffix_derived_from_base():
    assert _rewrite_suffix(REWRITE["ezproxy_login_url"]) == ".lib.ezproxy.hkust.edu.hk"
    # a login.* host still yields the same rewriting suffix
    assert _rewrite_suffix("https://login.lib.ezproxy.hkust.edu.hk/login") == ".lib.ezproxy.hkust.edu.hk"


def test_hostname_rewriting_dashes_target_host():
    out = _make_ezproxy_url("https://pubs.acs.org/doi/pdf/10.1021/x", REWRITE)
    assert out == "https://pubs-acs-org.lib.ezproxy.hkust.edu.hk/doi/pdf/10.1021/x"


def test_already_proxied_url_is_untouched():
    url = "https://pubs-acs-org.lib.ezproxy.hkust.edu.hk/doi/pdf/10.1021/x"
    assert _make_ezproxy_url(url, REWRITE) == url


def test_explicit_suffix_overrides_derived_one():
    cfg = {
        "ezproxy_login_url": "https://login.example.edu/login",
        "ezproxy_rewrite_suffix": ".proxy.example.edu",
    }
    assert _make_ezproxy_url("https://a.b.org/p", cfg) == "https://a-b-org.proxy.example.edu/p"


def test_no_base_yields_empty_url():
    assert _make_ezproxy_url("https://x.org/p", {}) == ""


def test_session_cookie_detection_ignores_guest_jars():
    guest = [{"name": "sd_session_id", "domain": ".sciencedirect.com"}]
    assert _has_ezproxy_session_cookie(guest) is False
    assert _has_ezproxy_session_cookie([{"name": "ezproxyl", "domain": ".lib.ezproxy.hkust.edu.hk"}]) is True


def test_sanitize_drops_unknown_fields_and_normalises_samesite():
    jar = [
        {
            "name": "ezproxyl",
            "value": "v",
            "domain": ".lib.ezproxy.hkust.edu.hk",
            "path": "/",
            "expires": -1,
            "sameSite": "no_restriction",
            "secure": False,
            "bogus": 1,
        },
        {"name": "", "value": "v", "domain": ".x.org"},  # dropped: no name
        {"name": "ESTSAUTH", "value": "v", "domain": ".login.microsoftonline.com", "sameSite": "unspecified"},
    ]
    out = _sanitize_playwright_cookies(jar)
    assert len(out) == 2
    first = out[0]
    assert "bogus" not in first and "expires" not in first
    # sameSite None requires secure, otherwise Chromium raises "Invalid cookie fields"
    assert first["sameSite"] == "None" and first["secure"] is True
    assert "sameSite" not in out[1]
