"""WebVPN institutional source — ``vpnsci`` naming aliases for ``instsci``.

v1.5.1 consolidated the original ``vpnsci.py`` into ``instsci.py``.  v1.7.0
renamed the user-facing surface (config keys, MCP tools, ``download()`` kwarg)
back to ``vpnsci`` while the implementation stayed as ``instsci.py``.  This
module restores the ``vpnsci`` import path by re-exporting the ``instsci``
implementation under both naming conventions, so callers using either name
keep working.
"""

from __future__ import annotations

from .instsci import (
    convert_url,
    _get_webvpn_base,
    _validate_session,
    instsci_cookie_path,
    instsci_is_configured,
    instsci_login,
    try_instsci,
)

# vpnsci-named aliases — the public surface expected by sources/__init__.py,
# server.py and config.py on v1.7.0+.
try_vpnsci = try_instsci
vpnsci_login = instsci_login
vpnsci_is_configured = instsci_is_configured
vpnsci_cookie_path = instsci_cookie_path

__all__ = [
    "try_vpnsci",
    "vpnsci_login",
    "vpnsci_is_configured",
    "vpnsci_cookie_path",
    "convert_url",
    "_validate_session",
    "_get_webvpn_base",
]
