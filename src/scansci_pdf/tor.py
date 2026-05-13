"""Tor SOCKS5 proxy integration with embedded Tor support."""

from __future__ import annotations

import os
from typing import Any

from .log import get_logger

log = get_logger()

DEFAULT_TOR_PROXY = "socks5h://127.0.0.1:1080"


def get_tor_proxy(config: dict[str, Any] | None = None) -> str | None:
    """Get Tor proxy URL.

    Priority:
    1. TOR_PROXY env var
    2. Config tor_proxy
    3. Default 1080 port
    """
    proxy = os.environ.get("TOR_PROXY", "")
    if proxy:
        return proxy
    if config and config.get("tor_proxy"):
        return config["tor_proxy"]
    return DEFAULT_TOR_PROXY


def check_tor_circuit(config: dict[str, Any] | None = None) -> bool:
    """Check if Tor SOCKS5 proxy is reachable."""
    import socket
    try:
        proxy = get_tor_proxy(config)
        if not proxy:
            return False
        host_port = proxy.split("://")[-1]
        host, port = host_port.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=3):
            return True
    except Exception:
        return False


def ensure_tor(config: dict[str, Any]) -> str | None:
    """Ensure Tor is available, starting embedded Tor if needed.

    Returns the SOCKS5 proxy URL or None if Tor is unavailable.
    """
    # 1. Check if external Tor is already running
    if check_tor_circuit(config):
        return get_tor_proxy(config)

    # 2. Try starting embedded Tor (auto-install if needed)
    try:
        from .embedded_tor import get_embedded_tor
        log.info("Tor: starting embedded Tor...")
        tor = get_embedded_tor(config)
        if tor and tor.is_running():
            log.info(f"Tor: embedded Tor started at {tor.proxy_url}")
            return tor.proxy_url
        log.warning("Tor: embedded Tor failed to start")
    except Exception as e:
        log.warning(f"Tor: embedded Tor error: {e}")

    return None


def start_embedded_tor(config: dict[str, Any]) -> dict[str, Any]:
    """Start embedded Tor proxy. Returns status dict."""
    try:
        from .embedded_tor import get_embedded_tor
        tor = get_embedded_tor(config)
        if tor:
            return {"running": True, "proxy": tor.proxy_url}
        return {"running": False, "error": "Failed to start embedded Tor"}
    except Exception as e:
        return {"running": False, "error": str(e)}


def stop_embedded_tor() -> dict[str, Any]:
    """Stop embedded Tor proxy."""
    try:
        from .embedded_tor import stop_embedded_tor as _stop
        _stop()
        return {"stopped": True}
    except Exception as e:
        return {"stopped": False, "error": str(e)}


def install_tor(config: dict[str, Any]) -> dict[str, Any]:
    """Download and install Tor Expert Bundle."""
    try:
        from .embedded_tor import install_tor as _install
        return _install(config)
    except Exception as e:
        return {"installed": False, "error": str(e)}
