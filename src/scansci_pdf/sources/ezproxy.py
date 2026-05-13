"""EZProxy institutional proxy source.

Uses the university library's EZProxy service to access papers.
EZProxy rewrites URLs through the library proxy, providing
institutional access to subscribed journals.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from ..log import get_logger
from ..pdf_utils import (
    _response_looks_pdf,
    extract_pdf_url_from_html,
    is_pdf_file,
    is_plausible_pdf_url,
    success,
)

log = get_logger()


def _get_ezproxy_base(config: dict[str, Any]) -> str:
    """Get EZProxy login URL template."""
    return config.get("ezproxy_login_url", "")


def _make_ezproxy_url(target_url: str, config: dict[str, Any]) -> str:
    """Convert a target URL to an EZProxy-proxied URL."""
    base = _get_ezproxy_base(config)
    if not base:
        return ""
    return base.replace("{url}", target_url)


def _validate_ezproxy_session(config: dict[str, Any]) -> bool:
    """Check if saved EZProxy cookies still work."""
    cookie_file = _ezproxy_cookie_path(config)
    if not cookie_file.exists():
        return False
    try:
        import json
        cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not cookies:
        return False

    sess = requests.Session()
    sess.trust_env = False
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))

    # Test with a known URL
    test_url = _make_ezproxy_url("https://www.sciencedirect.com", config)
    if not test_url:
        return False
    try:
        resp = sess.get(test_url, timeout=15, allow_redirects=True)
        # If redirected to login, session is invalid
        if "login" in resp.url.lower() or "libproxy" in resp.url.lower():
            return False
        return resp.status_code == 200
    except Exception:
        return False


def _ezproxy_cookie_path(config: dict[str, Any]) -> Path:
    """Get path to saved EZProxy cookies."""
    from ..config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    return cache_dir / "ezproxy_cookies.json"


def ezproxy_login(config: dict[str, Any]) -> bool:
    """Open browser for EZProxy login. Tries camoufox first, falls back to Selenium."""
    # Try camoufox (stealth browser) first
    try:
        from ..camofox_login import ezproxy_login as _camofox_ezproxy
        if _camofox_ezproxy(config):
            return True
    except Exception as exc:
        log.info(f"   [EZProxy] camoufox login failed: {exc}")

    # Fallback to Selenium
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
    except ImportError:
        log.info("   [EZProxy] selenium not installed")
        return False

    base = _get_ezproxy_base(config)
    if not base:
        log.info("   [EZProxy] No ezproxy_login_url configured")
        return False

    # Use a test URL to trigger login
    login_url = base.replace("{url}", "https://www.sciencedirect.com")
    log.info(f"   [EZProxy] Opening {login_url[:80]}...")

    options = Options()
    options.add_argument("--no-first-run")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-allow-origins=*")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        log.info(f"   [EZProxy] Chrome launch failed: {e}")
        return False

    try:
        driver.get(login_url)
        log.info("   [EZProxy] Please log in via your library credentials...")

        # Wait for login (up to 180 seconds)
        max_wait = 180
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(3)
            elapsed += 3
            try:
                url = driver.current_url
            except Exception:
                log.info("   [EZProxy] Browser closed by user.")
                return False

            # If redirected away from login page, login is complete
            if "libproxy" not in url.lower() and "login" not in url.lower():
                # Save cookies
                cookies = driver.get_cookies()
                cookie_file = _ezproxy_cookie_path(config)
                import json
                cookie_data = [
                    {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
                    for c in cookies
                ]
                cookie_file.write_text(
                    json.dumps(cookie_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                log.info(f"   [EZProxy] Login successful! Saved {len(cookie_data)} cookies.")
                return True

        log.info("   [EZProxy] Login timed out.")
        return False

    except Exception as e:
        log.info(f"   [EZProxy] Error: {e}")
        return False
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def try_ezproxy(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try downloading paper through EZProxy institutional proxy.

    Uses Selenium browser to access the paper through the library proxy,
    which handles authentication and cookie management automatically.
    """
    if not config.get("ezproxy_enabled", False):
        return None

    base = _get_ezproxy_base(config)
    if not base:
        return None

    # Resolve DOI to get publisher URL
    try:
        resp = requests.head(f"https://doi.org/{doi}", allow_redirects=True, timeout=10)
        resolved_url = resp.url
    except Exception:
        resolved_url = f"https://doi.org/{doi}"

    # Construct EZProxy URL
    ezproxy_url = _make_ezproxy_url(resolved_url, config)
    if not ezproxy_url:
        return None

    log.info(f"   [EZProxy] Trying {doi} via library proxy...")

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
    except ImportError:
        log.info("   [EZProxy] selenium not installed")
        return None

    download_dir = str(output_path.parent)
    options = Options()
    options.add_argument("--no-first-run")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-allow-origins=*")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        log.info(f"   [EZProxy] Chrome launch failed: {e}")
        return None

    try:
        # Load saved cookies if available
        cookie_file = _ezproxy_cookie_path(config)
        if cookie_file.exists():
            import json
            from urllib.parse import urlparse
            cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
            # Navigate to EZProxy base domain to set cookies
            parsed = urlparse(base)
            ezproxy_base = f"{parsed.scheme}://{parsed.hostname}"
            driver.get(ezproxy_base)
            time.sleep(1)
            for c in cookies:
                try:
                    params = {"name": c["name"], "value": c["value"], "path": c.get("path", "/")}
                    if c.get("domain"):
                        params["domain"] = c["domain"]
                    driver.execute_cdp_cmd("Network.setCookie", params)
                except Exception:
                    pass
            driver.execute_cdp_cmd("Network.enable", {})

        # Navigate to EZProxy URL
        driver.get(ezproxy_url)
        time.sleep(8)

        # Check if redirected to login
        url = driver.current_url
        if "libproxy" in url.lower() or "login" in url.lower():
            log.info("   [EZProxy] Login required. Please log in...")
            max_wait = 180
            elapsed = 0
            while elapsed < max_wait:
                time.sleep(3)
                elapsed += 3
                try:
                    url = driver.current_url
                except Exception:
                    return None
                if "libproxy" not in url.lower() and "login" not in url.lower():
                    break
            else:
                log.info("   [EZProxy] Login timed out.")
                return None

        # Check page content
        body = driver.execute_script("return document.body.innerText")
        if "robot" in body.lower() or "captcha" in body.lower():
            log.info("   [EZProxy] Bot detection triggered.")
            return None

        # Look for PDF download link
        links = driver.find_elements(By.CSS_SELECTOR, "a")
        for link in links:
            href = link.get_attribute("href") or ""
            text = link.text.strip().lower()
            if "pdf" in text and "purchase" not in text:
                log.info(f"   [EZProxy] Found PDF link: {href[:80]}")
                driver.get(href)
                time.sleep(5)
                # Check for downloaded file
                downloaded = _find_downloaded_ezproxy(download_dir, doi)
                if downloaded:
                    return success(doi, downloaded, "EZProxy")

        # Try pdfft pattern for ScienceDirect
        import re
        pii_match = re.search(r"pii/([A-Z0-9]+)", url)
        if pii_match:
            pdfft_url = f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}/pdfft"
            ezproxy_pdfft = _make_ezproxy_url(pdfft_url, config)
            driver.get(ezproxy_pdfft)
            time.sleep(5)
            downloaded = _find_downloaded_ezproxy(download_dir, doi)
            if downloaded:
                return success(doi, downloaded, "EZProxy")

    except Exception as e:
        log.info(f"   [EZProxy] Error: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return None


def _find_downloaded_ezproxy(download_dir: str, doi: str) -> Path | None:
    """Check download directory for recently downloaded PDF files."""
    dir_path = Path(download_dir)
    if not dir_path.exists():
        return None
    now = time.time()
    for f in dir_path.iterdir():
        if f.suffix.lower() == ".pdf" and (now - f.stat().st_mtime) < 30:
            try:
                if f.stat().st_size > 1000:
                    return f
            except OSError:
                pass
    return None
