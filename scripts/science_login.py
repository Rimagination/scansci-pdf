"""Science.org institutional login helper — auto-detects login completion.

Usage: python scripts/science_login.py

Opens a visible browser. Log in with your institution.
Script auto-detects login and saves cookies. Close browser when done.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scansci_pdf.camofox_login import (
    _save_cookies_json, _save_cookies_netscape, _import_to_camofox_browser,
)
from scansci_pdf.config import load_config, DATA_DIR

config = load_config()
cookie_file = Path(str(DATA_DIR)) / "cookies" / "science_login.json"

url = "https://www.science.org/doi/10.1126/science.aec6396"

print(f"Opening: {url}")
print("Log in with your institution. Script will auto-detect when done.")
print()

from camoufox.sync_api import Camoufox
from camoufox.addons import DefaultAddons

LOGIN_INDICATORS = [
    'sign out', 'signout', 'log out', 'logout',
    'my account', 'my profile', 'my settings',
    'institutional access', 'access provided by',
]
PAYWALL_INDICATORS = [
    'purchase', 'subscribe', 'buy this',
    'access through your institution',
]


def _check_logged_in(page) -> bool:
    """Check if page content indicates user is logged in."""
    try:
        text = (page.inner_text("body") or "").lower()[:5000]
    except Exception:
        return False

    has_login_indicator = any(s in text for s in LOGIN_INDICATORS)
    has_paywall = any(s in text for s in PAYWALL_INDICATORS)
    return has_login_indicator and not has_paywall


def _save_all(context, config, cookie_file):
    cookies = context.cookies()
    _save_cookies_json(cookies, cookie_file)
    netscape_path = cookie_file.with_suffix(".txt")
    _save_cookies_netscape(cookies, netscape_path)
    count = _import_to_camofox_browser(netscape_path, config)
    print(f"Captured {len(cookies)} cookies, imported {count} into camofox-browser")
    return cookies


with Camoufox(headless=False, exclude_addons=[DefaultAddons.UBO]) as browser:
    context = browser.new_context()
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    logged_in = False
    for i in range(180):  # 3 min max
        time.sleep(1)
        try:
            _ = page.url
        except Exception:
            print("\nBrowser closed.")
            break

        if not logged_in and _check_logged_in(page):
            logged_in = True
            print(f"\nLogin detected! Saving cookies...")
            _save_all(context, config, cookie_file)
            print("You can close the browser now.")
            break

        if (i + 1) % 10 == 0:
            print(".", end="", flush=True)

    if not logged_in:
        # Save whatever we have when browser closes or timeout
        print("\nSaving cookies from current session...")
        _save_all(context, config, cookie_file)

    # Final save
    cookies = _save_all(context, config, cookie_file)
    for c in cookies:
        print(f"  {c['name']}: {c['value'][:40]}")
    print("\nDone!")
