"""Config value masking: secrets and proxy credentials never leak via MCP."""

from __future__ import annotations

import pytest

from scansci_pdf import config as config_mod
from scansci_pdf.config import get_config_safe, mask_config_value


def test_mask_sensitive_key():
    assert mask_config_value("elsevier_api_key", "sk-secret-123") == "***"
    assert mask_config_value("zotero_api_key", "abc") == "***"
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
