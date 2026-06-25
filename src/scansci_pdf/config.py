"""Configuration management for ScanSci PDF."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("SCANSCI_PDF_DATA_DIR", str(Path.home() / ".scansci-pdf")))
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_SCIHUB_DOMAINS = [
    # Working mirrors (verified 2026-05-31, PDF via sci.bban.top)
    "https://sci-hub.mksa.top",
    "https://sci-hub.al",
    "https://sci-hub.mk",
    "https://sci-hub.vg",
    # Cloudflare/CAPTCHA protected (may work via CloakBrowser)
    "https://sci-hub.st",
    "https://sci-hub.ee",
    "https://sci-hub.ru",
    # Legacy (currently down, kept for future recovery)
    "https://sci-hub.se",
    "https://sci-hub.is",
    "https://sci-hub.41610.org",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "email": "scansci-pdf@example.invalid",
    "mcp_server_name": os.environ.get("MCP_SERVER_NAME", "scansci-pdf"),
    "output_dir": str(DATA_DIR / "papers"),
    "cache_dir": str(DATA_DIR / "cache"),
    "network_proxy": "",
    "proxy_only_scihub": False,
    "scihub_enabled": True,
    "scihub_domains": DEFAULT_SCIHUB_DOMAINS,
    "instsci_enabled": False,
    "instsci_school": "",
    "instsci_base_url": "",
    "instsci_cookie_file": "",
    "carsi_enabled": False,
    "carsi_idp_name": "",
    "ezproxy_enabled": False,
    "ezproxy_login_url": "",
    "core_api_key": "",
    "openalex_api_key": "",
    "elsevier_api_key": "",
    "elsevier_insttoken": "",
    "connect_timeout": 15,
    "read_timeout": 30,
    "request_delay_min": 2.0,
    "request_delay_max": 5.0,
    "fixed_request_delay_enabled": False,
    "json_probe_cache_seconds": 3600,
    "cache_ttl_hours": 168,
    "parallel_sources": True,
    "parallel_probes": True,
    "batch_workers": 10,
    "batch_stagger_seconds": 0.3,
    "min_pdf_size_bytes": 10000,
    "browser_headless": False,
    "browser_humanize": True,
    "browser_static_proxy": "",
    "browser_fingerprint_consistent": True,
    "remote_assist_port": 0,
    "is_campus_network": False,
    "tor_proxy": os.environ.get("TOR_PROXY", ""),
    "tor_use_bridges": False,
    "use_tor_for_scihub": True,
    "google_scholar_limit": 5,
    "max_browser_workers": 2,
    "host_concurrency": {},
    "auto_rename": True,
    "zotero_api_key": "",
    "zotero_library_type": "user",
    "zotero_library_id": "",
    "cookie_path": "",
    "chrome_profile_dir": "",
    "carsi_cookie_dir": "",
}


def load_config() -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
            if isinstance(existing, dict):
                config.update(existing)
        except Exception:
            pass
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)
    return config


def save_config(config: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)


_VALIDATION_RULES: dict[str, tuple[type, Any, Any]] = {
    # key: (type, min_value, max_value)
    "connect_timeout": (int, 1, 60),
    "read_timeout": (int, 1, 120),
    "request_delay_min": (float, 0, 10),
    "request_delay_max": (float, 0, 30),
    "json_probe_cache_seconds": (float, 0, 86400),
    "cache_ttl_hours": (float, 0, 8760),
    "batch_workers": (int, 1, 20),
    "batch_stagger_seconds": (float, 0, 60),
    "min_pdf_size_bytes": (int, 100, 10_000_000),
    "google_scholar_limit": (int, 1, 100),
}


def update_config(key: str, value: str) -> dict[str, Any]:
    config = load_config()
    if key in config:
        old_type = type(config[key])
        if old_type == bool:
            config[key] = value.lower() in ("true", "1", "yes")
        elif old_type == int:
            try:
                config[key] = int(value)
            except ValueError:
                raise ValueError(f"Invalid integer value for '{key}': '{value}'")
        elif old_type == float:
            try:
                config[key] = float(value)
            except ValueError:
                raise ValueError(f"Invalid float value for '{key}': '{value}'")
        else:
            config[key] = value
    else:
        config[key] = value

    if key in _VALIDATION_RULES:
        _, min_val, max_val = _VALIDATION_RULES[key]
        if config[key] < min_val or config[key] > max_val:
            config[key] = DEFAULT_CONFIG[key]

    save_config(config)
    return config


def get_config_safe() -> dict[str, Any]:
    config = load_config()
    sensitive_keys = ["core_api_key", "instsci_cookie_file", "zotero_api_key", "zotero_library_id", "elsevier_api_key", "elsevier_insttoken"]
    for key in sensitive_keys:
        if config.get(key):
            config[key] = "***"
    return config
