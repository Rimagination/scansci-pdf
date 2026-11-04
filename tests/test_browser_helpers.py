"""Tests for the optional-browser test helpers."""

from __future__ import annotations

import tomllib
from pathlib import Path

from tests._browser_test_helpers import is_cloakbrowser_license_error


def test_detects_license_error_by_message():
    assert is_cloakbrowser_license_error(
        RuntimeError("No CloakBrowser license key. Get one free: https://cloakbrowser.dev/")
    )


def test_detects_license_error_by_class_name():
    class CloakBrowserLicenseError(RuntimeError):
        pass

    assert is_cloakbrowser_license_error(CloakBrowserLicenseError("license missing"))


def test_ignores_other_errors():
    assert not is_cloakbrowser_license_error(RuntimeError("connection refused"))


def test_cloakbrowser_extra_is_capped_below_0_7():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    spec = project["project"]["optional-dependencies"]["cloakbrowser"][0]

    assert spec == "cloakbrowser>=0.5.9,<0.7"
