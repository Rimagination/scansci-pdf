"""Institutional session self-healing.

Before a download tries the institutional phase, validate the saved sessions
and, when ``auto_relogin`` is enabled, refresh expired ones by opening the
login browser. Conservative by design: only a session that previously existed
(cookies on disk) and is clearly expired triggers the browser; fresh users
and network-unreachable states never do.
"""

from __future__ import annotations

from typing import Any

from .log import get_logger

log = get_logger()


def _webvpn_configured(config: dict[str, Any]) -> bool:
    return bool(config.get("vpnsci_enabled", False)) and bool(
        config.get("vpnsci_base_url") or config.get("vpnsci_school")
    )


def ensure_webvpn_session(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the WebVPN session and refresh it when clearly expired.

    Returns a status report whose ``status`` is one of ``disabled``,
    ``valid``, ``none`` (no saved cookies — never auto-login), ``unreachable``
    (network failure — never auto-login), ``refreshed``, or ``login_failed``.
    """
    if not config.get("auto_relogin", True):
        return {"status": "disabled", "auto_relogin": False}
    from .sources.instsci import instsci_login, session_status

    status = session_status(config)
    if status in ("valid", "none", "unreachable"):
        return {"status": status, "auto_relogin": True}
    log.info("   [session-heal] WebVPN session expired, opening login browser...")
    ok = instsci_login(config)
    after = session_status(config)
    return {
        "status": "refreshed" if ok and after == "valid" else "login_failed",
        "auto_relogin": True,
        "validated_after": after,
    }


def ensure_institutional_sessions(config: dict[str, Any], *, use_vpnsci: bool = True) -> dict[str, Any]:
    """Validate/refresh institutional sessions before a download attempt.

    CARSI sessions self-heal inside ``CARSIClient.login`` (cookie freshness
    check with automatic browser fallback), so only WebVPN needs a hook here.
    """
    report: dict[str, Any] = {}
    if use_vpnsci and _webvpn_configured(config):
        report["webvpn"] = ensure_webvpn_session(config)
    return report
