"""Dependency checking and graceful degradation."""

from __future__ import annotations

import importlib
from typing import Any

from .log import get_logger

log = get_logger()

# Core dependencies (required)
CORE_DEPS = {
    "requests": "HTTP client",
    "bs4": "HTML parsing (beautifulsoup4)",
    "mcp": "MCP protocol",
    "typer": "CLI framework",
    "uvicorn": "ASGI server",
}

# Optional dependencies
OPTIONAL_DEPS = {
    "socks": "SOCKS proxy support (requests[socks])",
    "Crypto": "WebVPN AES encryption (pycryptodome)",
    "cloakbrowser": "Stealth browser for publisher access",
}

_feature_status: dict[str, bool] = {}


def check_dependency(module_name: str) -> bool:
    """Check if a Python module is available."""
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False


def check_all() -> dict[str, dict[str, Any]]:
    """Check all dependencies and return status report."""
    result = {"core": {}, "optional": {}}

    for module, desc in CORE_DEPS.items():
        available = check_dependency(module)
        result["core"][module] = {
            "available": available,
            "description": desc,
            "required": True,
        }

    for module, desc in OPTIONAL_DEPS.items():
        available = check_dependency(module)
        result["optional"][module] = {
            "available": available,
            "description": desc,
            "required": False,
        }

    return result


def is_feature_available(feature: str) -> bool:
    """Check if a feature's dependencies are met."""
    if feature in _feature_status:
        return _feature_status[feature]

    feature_deps = {
        "socks_proxy": ["socks"],
        "instsci": ["Crypto", "cloakbrowser"],
        "html_parse": ["bs4"],
    }

    deps = feature_deps.get(feature, [])
    available = all(check_dependency(d) for d in deps)
    _feature_status[feature] = available
    return available


def warn_missing(feature: str, module: str) -> None:
    """Log a warning when an optional dependency is missing."""
    log.warning(f"Feature '{feature}' disabled: install '{module}' to enable")


def print_status() -> None:
    """Print dependency status to stderr."""
    report = check_all()

    log.info("=== Dependency Status ===")

    all_ok = True
    for module, info in report["core"].items():
        status = "OK" if info["available"] else "MISSING"
        if not info["available"]:
            all_ok = False
        log.info(f"  [{status}] {module} - {info['description']}")

    if not all_ok:
        log.warning("Some core dependencies are missing! Install with: pip install scansci-pdf")

    has_optional = False
    for module, info in report["optional"].items():
        if info["available"]:
            status = "OK"
        else:
            status = "optional"
            has_optional = True
        log.info(f"  [{status}] {module} - {info['description']}")

    # cloakbrowser iterates on anti-detection patches fast; flag stale installs
    # so users know to upgrade even when the package is present.
    if report["optional"].get("cloakbrowser", {}).get("available"):
        try:
            from .cli import _cloakbrowser_version_status
            cb_status, cb_detail = _cloakbrowser_version_status()
            if cb_status == "outdated":
                log.warning(f"  [STALE] cloakbrowser {cb_detail}")
        except Exception:
            pass

    if has_optional:
        log.info("  Install optional deps: pip install scansci-pdf[fast,vpnsci]")

    log.info("========================")
