"""Config value masking: secrets and proxy credentials never leak via MCP."""

from __future__ import annotations

import pytest

from scansci_pdf import config as config_mod
from scansci_pdf.config import get_config_safe, mask_config_value


def test_mask_sensitive_key():
    assert mask_config_value("elsevier_api_key", "sk-secret-123") == "***"
    assert mask_config_value("zotero_api_key", "abc") == "***"
    assert mask_config_value("sciverse_api_key", "scv-secret") == "***"
    assert mask_config_value("sciverse_api_key", "") == ""
    assert mask_config_value("core_api_key", None) is None


def test_mask_proxy_url_hides_password():
    masked = mask_config_value("network_proxy", "http://user:secret@proxy.corp:8080")
    assert masked == "http://user:***@proxy.corp:8080"
    # socks5 with credentials too
    masked = mask_config_value("browser_static_proxy", "socks5://u:p@10.0.0.1:1080")
    assert masked == "socks5://u:***@10.0.0.1:1080"


def test_mask_leaves_plain_values_untouched():
    assert mask_config_value("email", "me@example.com") == "me@example.com"
    assert mask_config_value("scihub_enabled", True) is True
    # proxy without credentials stays as-is
    assert mask_config_value("network_proxy", "http://10.0.0.1:8080") == "http://10.0.0.1:8080"


def test_get_config_safe_masks_everything(monkeypatch):
    fake = {
        "elsevier_api_key": "sk-live",
        "network_proxy": "http://alice:hunter2@proxy.corp:8080",
        "email": "me@example.com",
        "batch_workers": 8,
    }
    monkeypatch.setattr(config_mod, "load_config", lambda: dict(fake))
    safe = get_config_safe()
    assert safe["elsevier_api_key"] == "***"
    assert safe["network_proxy"] == "http://alice:***@proxy.corp:8080"
    assert safe["email"] == "me@example.com"
    assert safe["batch_workers"] == 8


def test_credential_shaped_keys_added_later_are_masked():
    """An exact-match list only masks what someone remembered to add: the
    Springer and OpenAlex keys, cookie_path and carsi_cookie_dir all shipped to
    MCP clients in cleartext that way (#70)."""
    for key in (
        "springer_api_key",
        "openalex_api_key",
        "cookie_path",
        "carsi_cookie_dir",
        "camofox_api_key",
        "camofox_access_key",
    ):
        assert mask_config_value(key, "SENTINEL-VALUE") == "***", key


def test_no_default_config_key_returns_a_credential_in_cleartext():
    """Guards the whole surface rather than a hand-picked list, so adding a
    credential-shaped key cannot leak again."""
    import re

    secret_name = re.compile(r"key|token|secret|password|cookie|credential", re.I)
    leaked = [
        key
        for key in config_mod.DEFAULT_CONFIG
        if secret_name.search(key) and mask_config_value(key, "SENTINEL-VALUE") != "***"
    ]
    assert not leaked, f"credential-shaped config keys returned in cleartext: {leaked}"
