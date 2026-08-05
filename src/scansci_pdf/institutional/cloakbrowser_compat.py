"""Compatibility helpers for CloakBrowser runtime quirks."""

from __future__ import annotations

import os
import platform
from typing import Any


def ensure_cloakbrowser_platform_compatible(config_module: Any | None = None) -> bool:
    """Patch CloakBrowser platform detection when Windows reports no machine.

    Some Windows Python environments return an empty string from
    ``platform.machine()``. CloakBrowser supports Windows x64, but its lookup
    table cannot match that empty architecture value. We add the narrow missing
    lookup entry at runtime instead of modifying the third-party package.
    """
    if platform.system() != "Windows" or platform.machine():
        return False

    try:
        config = config_module
        if config is None:
            from cloakbrowser import config as config  # type: ignore[no-redef]
    except Exception:
        return False

    supported = getattr(config, "SUPPORTED_PLATFORMS", None)
    if not isinstance(supported, dict):
        return False

    if ("Windows", "") in supported:
        return False

    is_64bit_windows = bool(os.environ.get("ProgramFiles(x86)")) or bool(
        os.environ.get("PROCESSOR_ARCHITEW6432")
    )
    if not is_64bit_windows:
        return False

    supported[("Windows", "")] = supported.get(("Windows", "AMD64"), "windows-x64")
    return True
