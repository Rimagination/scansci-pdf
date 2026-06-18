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
    """Open browser for EZProxy login. Tries stealth browser first, falls back to Selenium."""
    # Try stealth browser (stealth browser) first
    try:
        from ..browser_login import ezproxy_login as _browser_ezproxy
        if _browser_ezproxy(config):
            return True
    except Exception as exc:
        log.info(f"   [EZProxy] stealth browser login failed: {exc}")

    log.error("   [EZProxy] CloakBrowser login failed, no fallback available")
    return False


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
        from cloakbrowser import launch
        from ..browser_engine import _build_browser_args
    except ImportError:
        log.info("   [EZProxy] cloakbrowser not installed")
        return None

    download_dir = str(output_path.parent)
    args = _build_browser_args(config)
    captured_pdf: list[bytes] = []

    def _on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            if "pdf" in ct:
                try:
                    body = response.body()
                    if body and len(body) > 5000:
                        captured_pdf.append(body)
                except Exception:
                    pass
        except Exception:
            pass

    browser = launch(headless=False, humanize=True, args=args)
    try:
        context = browser.new_context()
        page = context.new_page()
        page.on("response", _on_response)

        # Load saved cookies if available
        cookie_file = _ezproxy_cookie_path(config)
        if cookie_file.exists():
            import json
            cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
            if cookies:
                context.add_cookies(cookies)

        # Navigate to EZProxy URL
        page.goto(ezproxy_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(8)

        # Check if redirected to login
        url = page.url
        if "libproxy" in url.lower() or "login" in url.lower():
            log.info("   [EZProxy] Login required. Please log in...")
            max_wait = 180
            elapsed = 0
            while elapsed < max_wait:
                time.sleep(3)
                elapsed += 3
                try:
                    url = page.url
                except Exception:
                    return None
                if "libproxy" not in url.lower() and "login" not in url.lower():
                    break
            else:
                log.info("   [EZProxy] Login timed out.")
                return None

        # Check for captured PDF
        if captured_pdf:
            pdf_bytes = captured_pdf[-1]
            if pdf_bytes[:5] == b"%PDF-":
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(pdf_bytes)
                return success(doi, output_path, "EZProxy")

        # Look for PDF link in page
        pdf_link = page.evaluate("""() => {
            const links = document.querySelectorAll('a');
            for (const link of links) {
                const text = (link.innerText || '').toLowerCase();
                const href = link.href || '';
                if (text.includes('pdf') && !text.includes('purchase') && href) {
                    return href;
                }
            }
            return '';
        }""")
        if pdf_link:
            log.info(f"   [EZProxy] Found PDF link: {pdf_link[:80]}")
            captured_pdf.clear()
            page.goto(pdf_link, wait_until="commit", timeout=30000)
            time.sleep(5)
            if captured_pdf:
                pdf_bytes = captured_pdf[-1]
                if pdf_bytes[:5] == b"%PDF-":
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(pdf_bytes)
                    return success(doi, output_path, "EZProxy")

    except Exception as e:
        log.info(f"   [EZProxy] Error: {e}")
    finally:
        try:
            browser.close()
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
