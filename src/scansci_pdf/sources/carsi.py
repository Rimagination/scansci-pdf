"""CARSI (Shibboleth/SAML) federated authentication for publisher access.

Provides institutional login through CARSI federation, supporting
publishers like Elsevier, Springer Nature, Wiley, ACS, etc.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from ..config import DATA_DIR
from ..log import get_logger
from ..pdf_utils import write_pdf_bytes_atomic
from ..publisher_strategies import (
    _IDP_MAP,
    _AUTH_KEYWORDS,
    _AUTH_TITLES,
    _INSTITUTION_SEARCH_SELECTORS,
    _SSO_LINK_FINDER_JS,
    _INSTITUTION_CLICK_JS,
)

log = get_logger()


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _wait_or_cancel(
    cancel_event: threading.Event | None,
    timeout: float,
) -> bool:
    if cancel_event is None:
        time.sleep(max(0.0, timeout))
        return False
    return cancel_event.wait(max(0.0, timeout))


@contextlib.contextmanager
def _cancelable_lock(
    lock: threading.Lock,
    cancel_event: threading.Event | None,
):
    acquired = False
    while not acquired:
        if _cancelled(cancel_event):
            yield False
            return
        acquired = lock.acquire(timeout=0.1)
    try:
        yield True
    finally:
        lock.release()

_PUBLISHER_CONFIGS_FILE = DATA_DIR / "publisher_carsi.json"
_PKG_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PKG_PUBLISHER_CONFIGS_FILE = _PKG_DATA_DIR / "publisher_carsi.json"


@dataclass
class PublisherCARSIConfig:
    name: str
    domains: list[str]
    login_url: str
    search_selector: str
    result_selector: str
    success_url_pattern: str
    pdf_pattern: str


def _load_publisher_configs() -> dict[str, PublisherCARSIConfig]:
    # Try package data first, then user data dir
    config_file = _PKG_PUBLISHER_CONFIGS_FILE if _PKG_PUBLISHER_CONFIGS_FILE.exists() else _PUBLISHER_CONFIGS_FILE
    if not config_file.exists():
        return {}
    data = json.loads(config_file.read_text(encoding="utf-8"))
    configs = {}
    for key, val in data.items():
        configs[key] = PublisherCARSIConfig(**val)
    return configs


def detect_publisher(url: str) -> str | None:
    """Detect publisher key from a URL."""
    hostname = urlparse(url).hostname or ""
    configs = _load_publisher_configs()
    for key, cfg in configs.items():
        for domain in cfg.domains:
            if domain in hostname:
                return key
    return None


class CARSIClient:
    """Manages CARSI/Shibboleth federated authentication with academic publishers."""

    _login_lock = threading.Lock()

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._sessions: dict[str, requests.Session] = {}
        self._publisher_configs = _load_publisher_configs()
        self._cookie_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / "carsi_cookies"
        self._cookie_dir.mkdir(parents=True, exist_ok=True)

    def _cookie_path(self, publisher: str) -> Path:
        return self._cookie_dir / f"{publisher}.json"

    def _get_session(self, publisher: str) -> requests.Session:
        if publisher not in self._sessions:
            sess = requests.Session()
            sess.trust_env = False
            sess.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
            self._sessions[publisher] = sess
        return self._sessions[publisher]

    def login(self, publisher: str, force: bool = False) -> bool:
        """Ensure we have a valid CARSI session for the given publisher."""
        with self._login_lock:
            if not force and self._try_load_cookies(publisher):
                log.info(f"   [CARSI] Loaded saved cookies for {publisher}")
                return True
            log.info(f"   [CARSI] No valid session for {publisher}. Opening browser...")
            return self._browser_login(publisher)

    def fetch(self, url: str, **kwargs) -> requests.Response | None:
        """Fetch a URL using CARSI-authenticated session."""
        publisher = detect_publisher(url)
        if not publisher:
            return None

        if not self.login(publisher):
            return None

        sess = self._get_session(publisher)
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        try:
            return sess.get(url, **kwargs)
        except requests.RequestException as e:
            log.warning(f"   [CARSI] Fetch failed: {e}")
            return None

    def download_via_browser(
        self,
        doi: str,
        article_url: str,
        output_path: Path,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        """Download PDF via CloakBrowser with CARSI auth."""
        return self._download_via_cloakbrowser(
            doi,
            article_url,
            output_path,
            cancel_event=cancel_event,
        )

    def _download_via_cloakbrowser(
        self,
        doi: str,
        article_url: str,
        output_path: Path,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        """Download PDF via CloakBrowser with CARSI auth. Single session: login + download."""
        if _cancelled(cancel_event):
            return None
        publisher = detect_publisher(article_url)
        if not publisher:
            return None
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return None

        try:
            from ..cloakbrowser_compat import prepare_cloakbrowser_runtime

            prepare_cloakbrowser_runtime()
            from cloakbrowser import launch  # noqa: F401
        except Exception:
            log.info("   [CARSI-Browser] cloakbrowser not installed")
            return None

        idp_name = self.config.get("carsi_idp_name", "")
        if not idp_name:
            log.info("   [CARSI-Browser] No carsi_idp_name configured")
            return None

        idp_en = _IDP_MAP.get(idp_name, idp_name)

        from ..pdf_utils import is_pdf_file, success as _success

        # Serialize browser opens across threads — only one browser at a time
        with _cancelable_lock(self._login_lock, cancel_event) as lock_acquired:
            if not lock_acquired:
                return None
            log.info(f"   [CARSI-Browser] Opening browser for {publisher}...")
            page = None
            captured_pdf: list[bytes] = []
            capture_lock = threading.Lock()
            capture_in_progress = False
            on_response = None
            try:
                from ..publisher_strategies import _visible_browser, _save_all_cookie_formats

                with _visible_browser(
                    self.config,
                    publisher,
                    viewport=None,
                    cancel_event=cancel_event,
                ) as (context, page):
                    def _try_save_captured() -> dict[str, Any] | None:
                        """If a PDF was captured, save and validate it."""
                        if _cancelled(cancel_event):
                            return None
                        if captured_pdf:
                            if write_pdf_bytes_atomic(
                                output_path,
                                captured_pdf[-1],
                                cancel_event,
                            ) and is_pdf_file(output_path):
                                return _success(doi, output_path, "CARSI-Browser")
                        return None

                    # Restore saved cookies if any (supplements persistent profile)
                    cookie_file = self._cookie_path(publisher)
                    if cookie_file.exists():
                        try:
                            saved = json.loads(cookie_file.read_text(encoding="utf-8"))
                            pw_cookies = []
                            for c in saved:
                                pw_c = {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
                                if pw_c["domain"]:
                                    pw_cookies.append(pw_c)
                            if pw_cookies:
                                context.add_cookies(pw_cookies)
                                log.info(f"   [CARSI-Browser] Restored {len(pw_cookies)} cookies from file")
                        except Exception:
                            pass

                    # Capture PDF from network
                    def on_response(response):
                        nonlocal capture_in_progress
                        if _cancelled(cancel_event):
                            return
                        try:
                            ct = response.headers.get("content-type", "")
                            url = response.url
                            is_pdf_ct = "pdf" in ct.lower() or "octet-stream" in ct.lower()
                            is_pdf_url = url.lower().endswith(".pdf") or "/pdfdirect/" in url or "/doi/pdf/" in url
                            if not (is_pdf_ct or is_pdf_url):
                                return
                            if response.status >= 400:
                                return
                            with capture_lock:
                                if _cancelled(cancel_event) or capture_in_progress or captured_pdf:
                                    return
                                capture_in_progress = True
                            try:
                                body = response.body()
                                if _cancelled(cancel_event) or captured_pdf:
                                    return
                                if len(body) > 5000 and body.startswith(b"%PDF-"):
                                    captured_pdf.append(body)
                                    log.info(f"   [CARSI-Browser] PDF captured: {len(body)} bytes")
                            finally:
                                with capture_lock:
                                    capture_in_progress = False
                        except Exception:
                            pass
                    page.on("response", on_response)

                    # Step 1: Navigate to article page first (gets Cloudflare clearance)
                    log.info(f"   [CARSI-Browser] Loading article: {article_url[:60]}")
                    if _cancelled(cancel_event):
                        return None
                    try:
                        page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    if _wait_or_cancel(cancel_event, 5):
                        return None

                    title = page.title()
                    url = page.url
                    log.info(f"   [CARSI-Browser] Page: '{title[:40]}' {url[:60]}")

                    # Wait for Cloudflare challenge to resolve (visible stealth browser can pass it)
                    from ..network import is_cloudflare_challenge
                    for _cf_wait in range(12):
                        if _cancelled(cancel_event):
                            return None
                        if is_cloudflare_challenge(page.title() or ""):
                            log.info(f"   [CARSI-Browser] Cloudflare challenge detected, waiting... ({_cf_wait+1}/12)")
                            if _wait_or_cancel(cancel_event, 5):
                                return None
                        else:
                            break
                    else:
                        log.info("   [CARSI-Browser] Cloudflare challenge did not resolve")

                    # Step 1b: Check if restored cookies already grant access
                    has_cookies = cookie_file.exists()
                    needs_login = False
                    try:
                        needs_login = page.evaluate("""
                            () => {
                                if (!document.body) return true;
                                const body = (document.body.innerText || '').toLowerCase();
                                const hasPaywall = body.includes('purchase') || body.includes('subscribe')
                                    || body.includes('access through your institution')
                                    || body.includes('sign in to access')
                                    || body.includes('buy this article');
                                const hasPdf = !!document.querySelector('a[href*="pdf"], a[href*="download"], iframe[src*="pdf"]');
                                return hasPaywall && !hasPdf;
                            }
                        """)
                    except Exception as _e:
                        log.info(f"   [CARSI-Browser] paywall check error (likely Cloudflare): {_e}")
                        needs_login = True

                    # Even if page looks accessible, verify cookies work by
                    # trying a quick pdfft probe. ScienceDirect may accept
                    # expired cookies without showing a paywall, but return
                    # HTML instead of PDF for /pdfft requests.
                    cookies_valid = False
                    if has_cookies and not needs_login:
                        pii_from_url = ""
                        _pm = re.search(r"pii/([A-Z0-9]+)", page.url)
                        if _pm:
                            pii_from_url = _pm.group(1)
                        if pii_from_url:
                            try:
                                probe_ok = page.evaluate(f"""
                                    (async () => {{
                                        try {{
                                            const r = await fetch('/science/article/pii/{pii_from_url}/pdfft',
                                                {{credentials: 'include', headers: {{'Accept': 'application/pdf'}}}});
                                            const ct = r.headers.get('content-type') || '';
                                            return r.ok && ct.includes('pdf');
                                        }} catch(e) {{ return false; }}
                                    }})()
                                """)
                                cookies_valid = bool(probe_ok)
                                if cookies_valid:
                                    log.info("   [CARSI-Browser] Cookie probe OK, skipping login")
                                else:
                                    log.info("   [CARSI-Browser] Cookie probe failed, re-login needed")
                            except Exception:
                                log.info("   [CARSI-Browser] Cookie probe error, will re-login")

                    if not cookies_valid:
                        # Step 2: Navigate to "Institutional login" link on article page
                        if _cancelled(cancel_event):
                            return None
                        sso_href = page.evaluate(_SSO_LINK_FINDER_JS)
                        if sso_href:
                            log.info(f"   [CARSI-Browser] Navigating to SSO: {sso_href[:80]}")
                            if _cancelled(cancel_event):
                                return None
                            try:
                                page.goto(sso_href, wait_until="domcontentloaded", timeout=30000)
                            except Exception:
                                pass
                        else:
                            log.info("   [CARSI-Browser] No SSO link found, trying direct login URL...")
                            if _cancelled(cancel_event):
                                return None
                            try:
                                page.goto(cfg.login_url, wait_until="domcontentloaded", timeout=30000)
                            except Exception:
                                pass

                        if _wait_or_cancel(cancel_event, 8):
                            return None

                        # Step 3: Search for institution in the WAYF page
                        search_input = page.query_selector('#searchInstitution')
                        if not search_input:
                            for sel in _INSTITUTION_SEARCH_SELECTORS[1:]:  # skip #searchInstitution (already tried)
                                search_input = page.query_selector(sel)
                                if search_input:
                                    break

                        if search_input:
                            search_input.fill(idp_en)
                            if _wait_or_cancel(cancel_event, 3):
                                return None
                            log.info(f"   [CARSI-Browser] Searched for '{idp_en}'")

                            # Click matching institution
                            clicked = page.evaluate(_INSTITUTION_CLICK_JS, idp_en)
                            if clicked:
                                log.info(f"   [CARSI-Browser] Selected: {clicked}")
                                if _wait_or_cancel(cancel_event, 5):
                                    return None
                            else:
                                search_input.press("Enter")
                                if _wait_or_cancel(cancel_event, 3):
                                    return None
                        else:
                            log.info("   [CARSI-Browser] No institution search box found")

                        # Step 4: Wait for CAS login
                        _ak = _AUTH_KEYWORDS
                        _at = _AUTH_TITLES

                        url = page.url
                        title = page.title()
                        if any(x in url.lower() for x in _ak) or any(x in title for x in _at):
                            log.info("   [CARSI-Browser] CAS login required. Please log in...")
                            for i in range(100):
                                if _wait_or_cancel(cancel_event, 3):
                                    return None
                                try:
                                    title = page.title()
                                    url = page.url
                                except Exception:
                                    return None
                                is_auth = any(x in title for x in _at)
                                is_auth_url = any(x in url.lower() for x in _ak)
                                if not is_auth and not is_auth_url:
                                    # Login success - save cookies in all formats + bridge to CloakBrowser
                                    try:
                                        cookies = context.cookies()
                                        _save_all_cookie_formats(cookies, publisher, self.config)
                                    except Exception:
                                        pass
                                    break
                            else:
                                log.info("   [CARSI-Browser] Login timed out")
                                return None
                        else:
                            log.info("   [CARSI-Browser] Already authenticated")

                    # Step 5: Navigate to article (with CARSI auth now)
                    if _wait_or_cancel(cancel_event, 2):
                        return None
                    log.info(f"   [CARSI-Browser] Navigating to article: {article_url[:60]}")
                    if _cancelled(cancel_event):
                        return None
                    try:
                        page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    if _wait_or_cancel(cancel_event, 5):
                        return None

                    # Check for PDF via network capture
                    saved = _try_save_captured()
                    if saved:
                        return saved

                    # Step 6: Try direct PDF URL
                    pii_match = re.search(r"pii/([A-Z0-9]+)", page.url)
                    pii_value = pii_match.group(1) if pii_match else ""
                    pdf_pattern = cfg.pdf_pattern.replace("{doi}", doi).replace("{pii}", pii_value)
                    if pdf_pattern and not pdf_pattern.startswith("http"):
                        pdf_url = f"https://{cfg.domains[0]}{pdf_pattern}"
                    else:
                        pdf_url = pdf_pattern

                    if pdf_url and "{pii}" not in pdf_url:
                        log.info(f"   [CARSI-Browser] Trying PDF: {pdf_url[:80]}")
                        captured_pdf.clear()
                        if _cancelled(cancel_event):
                            return None
                        try:
                            page.goto(pdf_url, wait_until="commit", timeout=30000)
                        except Exception:
                            pass
                        if _wait_or_cancel(cancel_event, 5):
                            return None
                        saved = _try_save_captured()
                        if saved:
                            return saved

                    # Step 7: Find PDF link in HTML
                    from ..pdf_utils import extract_pdf_url_from_html
                    if _cancelled(cancel_event):
                        return None
                    html = page.content()
                    if _cancelled(cancel_event):
                        return None
                    found_pdf = extract_pdf_url_from_html(html, page.url)
                    if found_pdf:
                        log.info(f"   [CARSI-Browser] Found PDF link: {found_pdf[:80]}")
                        captured_pdf.clear()
                        if _cancelled(cancel_event):
                            return None
                        try:
                            page.goto(found_pdf, wait_until="commit", timeout=30000)
                        except Exception:
                            pass
                        if _wait_or_cancel(cancel_event, 5):
                            return None
                        saved = _try_save_captured()
                        if saved:
                            return saved

                    # Step 8: Click PDF button
                    if _cancelled(cancel_event):
                        return None
                    click_result = page.evaluate("""
                        () => {
                            const links = document.querySelectorAll('a');
                            for (const a of links) {
                                const href = (a.getAttribute('href') || '').toLowerCase();
                                const text = (a.innerText || '').toLowerCase();
                                if ((href.includes('pdf') || href.includes('download')) && !href.includes('supplement')) {
                                    if (text.includes('pdf') || text.includes('download')) {
                                        a.click();
                                        return a.href;
                                    }
                                }
                            }
                            return null;
                        }
                    """)
                    if click_result:
                        log.info(f"   [CARSI-Browser] Clicked: {str(click_result)[:80]}")
                        if _wait_or_cancel(cancel_event, 8):
                            return None
                        saved = _try_save_captured()
                        if saved:
                            return saved

                    if _cancelled(cancel_event):
                        return None
                    log.info(f"   [CARSI-Browser] No PDF found. Title: {page.title()[:40]} URL: {page.url[:60]}")
                    return None

            except Exception as e:
                if not _cancelled(cancel_event):
                    log.info(f"   [CARSI-Browser] Error: {e}")
                return None
            finally:
                if page is not None and on_response is not None:
                    try:
                        page.remove_listener("response", on_response)
                    except Exception:
                        pass
                with capture_lock:
                    captured_pdf.clear()

    def _find_downloaded_pdf(self, download_dir: str, doi: str) -> Path | None:
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

    def _try_load_cookies(self, publisher: str) -> bool:
        cookie_file = self._cookie_path(publisher)
        if not cookie_file.exists():
            return False
        try:
            cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False

        sess = self._get_session(publisher)
        for cookie in cookies:
            sess.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
        return self._validate_session(publisher)

    def _validate_session(self, publisher: str) -> bool:
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return False
        sess = self._get_session(publisher)

        # Check cookie file freshness — accept if < 24h old
        cookie_file = self._cookie_path(publisher)
        try:
            age_hours = (time.time() - os.path.getmtime(cookie_file)) / 3600
            if age_hours > 24:
                log.info(f"   [CARSI] Cookies for {publisher} expired ({age_hours:.1f}h old)")
                return False
        except OSError:
            return False

        # Validate by hitting a publisher page that requires auth
        # Use the main domain, not login_url (which always contains "login")
        resp = None
        try:
            test_url = f"https://{cfg.domains[0]}/"
            resp = sess.get(test_url, timeout=15, allow_redirects=True)
            # If we get redirected to a SSO/CAS/WAYF page, session is invalid
            url_lower = resp.url.lower()
            sso_keywords = ("wayf", "shibboleth", "saml", "idp.bayern", "passport")
            if any(k in url_lower for k in sso_keywords):
                return False
            return resp.status_code == 200
        except requests.RequestException:
            return False
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    def _browser_login(self, publisher: str) -> bool:
        """Login via CARSI using CloakBrowser."""
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            log.error(f"   [CARSI] Unknown publisher: {publisher}")
            return False

        try:
            from ..browser_login import carsi_login
            return carsi_login(publisher, self.config, login_url=cfg.login_url, domains=cfg.domains)
        except Exception as exc:
            log.error(f"   [CARSI] CloakBrowser login failed: {exc}")
            return False

    def _extract_chrome_cookies(self, publisher: str) -> None:
        """Try to extract cookies from Chrome's cookie database."""
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return

        cookie_paths = [
            Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Cookies",
            Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies",
        ]

        for cookie_path in cookie_paths:
            if not cookie_path.exists():
                continue
            try:
                import shutil
                import sqlite3
                tmp_cookie = self._cookie_dir / "chrome_cookies_tmp.db"
                shutil.copy2(cookie_path, tmp_cookie)

                conn = sqlite3.connect(str(tmp_cookie))
                cursor = conn.cursor()

                cookies = []
                for domain in cfg.domains:
                    cursor.execute(
                        "SELECT name, value, host_key, path FROM cookies WHERE host_key LIKE ?",
                        (f"%{domain}%",),
                    )
                    cookies.extend(cursor.fetchall())
                conn.close()
                tmp_cookie.unlink(missing_ok=True)

                if cookies:
                    cookie_file = self._cookie_path(publisher)
                    cookie_data = [
                        {"name": n, "value": v, "domain": h, "path": p}
                        for n, v, h, p in cookies
                    ]
                    cookie_file.write_text(
                        json.dumps(cookie_data, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    log.info(f"   [CARSI] Extracted {len(cookie_data)} cookies from Chrome")
                    return
            except Exception as e:
                log.warning(f"   [CARSI] Chrome cookie extraction failed: {e}")

    def close(self):
        try:
            for sess in self._sessions.values():
                with contextlib.suppress(Exception):
                    sess.close()
        finally:
            self._sessions.clear()
