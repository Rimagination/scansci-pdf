"""Shared helpers for tests that launch an optional browser backend."""

from __future__ import annotations


def is_cloakbrowser_license_error(exc: BaseException) -> bool:
    """True when CloakBrowser refused to launch because no license key is set.

    cloakbrowser 0.7+ requires a free license key; the optional extra is capped
    below 0.7, but a user may have installed a newer version globally. These
    tests exercise a real browser, so skip instead of failing on a missing key.
    """
    text = str(exc)
    return (
        type(exc).__name__ == "CloakBrowserLicenseError"
        or "No CloakBrowser license key" in text
        or "cloakbrowser login" in text.lower()
    )
