"""Configuration management for ScanSci PDF."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("SCANSCI_PDF_DATA_DIR", str(Path.home() / ".scansci-pdf")))
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_SCIHUB_DOMAINS = [
    # Direct PDF mirrors (no CAPTCHA, PDF via sci.bban.top iframe) — verified 2026-07
    "https://sci-hub.vg",
    "https://sci-hub.al",
    "https://sci-hub.mk",
    # ALTCHA-protected (stable; used as manual-download hint)
    "https://sci-hub.ru",
    "https://sci-hub.ee",
    # Cloudflare-protected (requires CloakBrowser JS challenge bypass)
    "https://sci-hub.st",
    # Reported intermittent (2026-06); kept but deprioritized
    "https://sci-hub.mksa.top",
    # Legacy (currently down, kept for future recovery)
    "https://sci-hub.se",
    "https://sci-hub.is",
    "https://sci-hub.41610.org",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "email": "scansci-pdf@example.invalid",
    "output_dir": str(DATA_DIR / "papers"),
    "cache_dir": str(DATA_DIR / "cache"),
    "network_proxy": "",
    "proxy_pool": "",  # 逗号分隔的代理列表；非空时批量下载按代理轮换出口 IP
    "download_strategy": "fastest",  # fastest / grey_only / scihub_only / scihub_first / oa_first / legal_only / ezproxy_only
    "scihub_enabled": True,
    "scihub_domains": DEFAULT_SCIHUB_DOMAINS,
    "vpnsci_enabled": False,
    "vpnsci_school": "",
    "vpnsci_base_url": "",
    "vpnsci_cookie_file": "",
    "carsi_enabled": False,
    "carsi_idp_name": "",
    "ezproxy_enabled": False,
    "ezproxy_login_url": "",
    "ezproxy_challenge_timeout": 120,
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
    "is_campus_network": False,
    "tor_proxy": os.environ.get("TOR_PROXY", ""),
    "tor_use_bridges": False,
    "use_tor_for_scihub": True,
    "google_scholar_limit": 5,
    "max_browser_workers": 1,
    "scihub_browser_workers": 3,  # Number of Sci-Hub domains to race in parallel via browser
    "host_concurrency": {},
    "auto_rename": True,
    "zotero_api_key": "",
    "zotero_library_type": "user",
    "zotero_library_id": "",
    "flaresolverr_url": "http://127.0.0.1:8191/v1",
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
    "ezproxy_challenge_timeout": (int, 15, 600),
    "request_delay_min": (float, 0.0, 30.0),
    "request_delay_max": (float, 0.0, 60.0),
    "json_probe_cache_seconds": (int, 0, 86400),
    "cache_ttl_hours": (int, 1, 8760),
    "batch_workers": (int, 1, 50),
    "batch_stagger_seconds": (float, 0.0, 10.0),
    "min_pdf_size_bytes": (int, 100, 1000000),
    "google_scholar_limit": (int, 1, 50),
}

_VALID_STRATEGIES = frozenset(
    {"fastest", "scihub_first", "scihub_only", "grey_only", "oa_first", "legal_only", "ezproxy_only"}
)


def update_config(key: str, value: str) -> dict[str, Any]:
    import warnings as _warnings

    config = load_config()

    # Validate: warn on unknown keys
    if key not in DEFAULT_CONFIG:
        _warnings.warn(
            f"Unknown config key '{key}' — it will be stored but may have no effect. "
            f"Valid keys include: {', '.join(sorted(DEFAULT_CONFIG.keys()))}",
            stacklevel=2,
        )

    # Special handling for download_strategy
    if key == "download_strategy":
        value_lower = value.lower().strip()
        if value_lower not in _VALID_STRATEGIES:
            raise ValueError(
                f"Invalid download_strategy '{value}'. Valid options: {', '.join(sorted(_VALID_STRATEGIES))}"
            )
        config[key] = value_lower
        save_config(config)
        return config

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
        elif old_type == list:
            # Try JSON parse for list values (e.g. scihub_domains)
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    config[key] = parsed
                else:
                    raise ValueError(f"Expected a JSON list for '{key}', got {type(parsed).__name__}")
            except json.JSONDecodeError:
                raise ValueError(
                    f"Invalid list value for '{key}'. Use JSON format, e.g.: '[\"https://sci-hub.vg\",\"https://sci-hub.al\"]'"
                )
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
    sensitive_keys = ["core_api_key", "vpnsci_cookie_file", "zotero_api_key", "zotero_library_id", "elsevier_api_key", "elsevier_insttoken"]
    for key in sensitive_keys:
        if config.get(key):
            config[key] = "***"
    return config


def parse_proxy_pool(value: str | None) -> list[str]:
    """Parse a comma-separated proxy list into a deduplicated list.

    Accepts forms like ``"socks5://1.1.1.1:1080, http://2.2.2.2:8080"`` and
    returns ``["socks5://1.1.1.1:1080", "http://2.2.2.2:8080"]``. Empty/blank
    entries are dropped. Order is preserved; duplicates removed.
    """
    if not value:
        return []
    seen: set[str] = set()
    proxies: list[str] = []
    for token in str(value).split(","):
        proxy = token.strip()
        if proxy and proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies
