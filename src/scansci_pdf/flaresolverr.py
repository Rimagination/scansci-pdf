"""Anti-bot bypass — delegates to camofox-browser.

This module retains the flaresolverr.py filename for caller compatibility.
All actual bypass logic goes through camofox.
"""

from __future__ import annotations

from typing import Any

from .log import get_logger

log = get_logger()

_DEFAULT_TIMEOUT = 60000


def is_available(config: dict[str, Any]) -> bool:
    """Check if camofox-browser is reachable."""
    from .camofox import is_available as _camofox_avail
    return _camofox_avail(config)


def solve_url(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = _DEFAULT_TIMEOUT,
    session_id: str | None = None,
    skip_curl_cffi: bool = False,
) -> dict[str, Any] | None:
    """Solve anti-bot challenges via camofox-browser."""
    if not config.get("camofox_url"):
        return None
    from .camofox import solve_url as _camofox_solve
    log.info(f"camofox: solving {url}")
    result = _camofox_solve(url, config, max_timeout=max_timeout)
    if result:
        log.info("camofox: solved")
    return result


def get_cookies(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = _DEFAULT_TIMEOUT,
) -> dict[str, str] | None:
    """Solve and return cookies as a dict."""
    from .camofox import get_cookies as _camofox_cookies
    return _camofox_cookies(url, config, max_timeout=max_timeout)


def get_html(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = _DEFAULT_TIMEOUT,
) -> str | None:
    """Solve and return page HTML."""
    from .camofox import get_html as _camofox_html
    return _camofox_html(url, config, max_timeout=max_timeout)


def create_session(config: dict[str, Any], session_id: str) -> bool:
    """No-op — camofox manages sessions internally."""
    return is_available(config)


def destroy_session(config: dict[str, Any], session_id: str) -> None:
    """No-op — camofox manages sessions internally."""
    from .camofox import close_all_tabs
    close_all_tabs(config)
