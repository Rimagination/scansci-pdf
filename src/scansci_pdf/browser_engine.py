"""Browser engine: CloakBrowser-based replacement for camofox daemon API.

Provides the same public API as the old camofox.py (is_available, solve_url,
get_cookies, get_html, import_cookies, evaluate_js, create_tab, close_tab,
navigate_tab, get_snapshot, download_pdf_via_browser, fetch_url,
get_captured_responses, close_all_tabs) but uses CloakBrowser's direct
Playwright API instead of HTTP calls to an external daemon.

Uses a single shared browser instance with multiple tabs (pages). Cookies,
login sessions, and Cloudflare bypass state are shared across all tabs.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CloakBrowser availability check
# ---------------------------------------------------------------------------

_HAS_CLOAKBROWSER: bool | None = None


def _check_cloakbrowser() -> bool:
    global _HAS_CLOAKBROWSER
    if _HAS_CLOAKBROWSER is None:
        try:
            from cloakbrowser import launch  # noqa: F401
            _HAS_CLOAKBROWSER = True
        except ImportError:
            _HAS_CLOAKBROWSER = False
    return _HAS_CLOAKBROWSER


# ---------------------------------------------------------------------------
# Per-thread browser (one browser per thread, multiple tabs per browser)
# Playwright sync API uses greenlets bound to threads — cross-thread
# usage crashes. Each thread gets its own browser; within a thread,
# tabs share cookies/session/Cloudflare state.
# ---------------------------------------------------------------------------

import threading as _threading

_tls = _threading.local()


def _build_browser_args(config: dict[str, Any] | None = None) -> list[str]:
    """Build Chromium launch args from config (proxy, flags, etc.)."""
    args = ["--disable-features=CrossOriginOpenerPolicy"]
    if config:
        proxy = config.get("browser_static_proxy", "")
        if proxy:
            args.append(f"--proxy-server={proxy}")
    return args


def _get_shared_browser(config: dict[str, Any] | None = None):
    """Get or create a browser for the current thread. Returns (browser, context)."""
    browser = getattr(_tls, "browser", None)
    context = getattr(_tls, "context", None)
    if browser is not None:
        return browser, context

    # Playwright's Sync API cannot run inside a running asyncio event loop.
    # Detect that case and bail out early with a clear message instead of
    # hitting a confusing "asyncio.run() cannot be called from a running
    # event loop" deep inside CloakBrowser.
    try:
        import asyncio
        asyncio.get_running_loop()
        raise RuntimeError(
            "CloakBrowser (Playwright Sync API) cannot run inside an asyncio "
            "event loop. Use the HTTP download sources instead, or call from "
            "a synchronous context."
        )
    except RuntimeError as e:
        if "cannot run inside" in str(e):
            raise
        # No running loop — safe to proceed.

    if not _check_cloakbrowser():
        raise RuntimeError("cloakbrowser not installed. Run: pip install cloakbrowser")

    # Platform compat shim
    try:
        from ..institutional.cloakbrowser_compat import ensure_cloakbrowser_platform_compatible
        ensure_cloakbrowser_platform_compatible()
    except Exception:
        try:
            from .institutional.cloakbrowser_compat import ensure_cloakbrowser_platform_compatible
            ensure_cloakbrowser_platform_compatible()
        except Exception:
            pass

    from cloakbrowser import launch

    headless = False
    humanize = True
    if config:
        headless = config.get("browser_headless", False)
        humanize = config.get("browser_humanize", True)

    args = _build_browser_args(config)
    browser = launch(headless=headless, humanize=humanize, args=args)
    context = browser.new_context()
    _tls.browser = browser
    _tls.context = context
    logger.info(f"browser_engine: browser ready for thread {_threading.current_thread().name}")
    return browser, context


def get_persistent_context(
    profile_dir: str | Path,
    config: dict[str, Any] | None = None,
):
    """Get or create a persistent browser context for fingerprint consistency.

    Unlike launch() + cookie restore, persistent context preserves:
    - Browser fingerprint (canvas, WebGL, audio, fonts)
    - Cookies and localStorage across restarts
    - Login sessions without re-authentication

    This is the recommended approach for publisher sessions that need
    stable identity across multiple download runs.
    """
    if not _check_cloakbrowser():
        raise RuntimeError("cloakbrowser not installed. Run: pip install cloakbrowser")

    try:
        from .cloakbrowser_compat import prepare_cloakbrowser_runtime
        prepare_cloakbrowser_runtime()
    except Exception:
        pass

    from cloakbrowser import launch_persistent_context

    headless = False
    humanize = True
    if config:
        headless = config.get("browser_headless", False)
        humanize = config.get("browser_humanize", True)

    args = _build_browser_args(config)
    profile_path = Path(profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)

    ctx = launch_persistent_context(
        str(profile_path),
        headless=headless,
        humanize=humanize,
        args=args,
    )
    logger.info(f"browser_engine: persistent context ready at {profile_path}")
    return ctx


def get_browser_page(config: dict[str, Any] | None = None):
    """Get a new page from the shared browser (for custom browser interactions).
    
    Returns a Playwright Page object that the caller must close after use.
    Returns None if the browser is not available.
    """
    if not _check_cloakbrowser():
        return None
    try:
        _browser, context = _get_shared_browser(config)
        return context.new_page()
    except Exception:
        return None


def shutdown_shared_browser():
    """Shut down the current thread's browser. Call on thread exit or process exit."""
    browser = getattr(_tls, "browser", None)
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
        _tls.browser = None
        _tls.context = None
        logger.info("browser_engine: browser shut down")


def _ensure_compat():
    """Ensure CloakBrowser platform compatibility."""
    try:
        from ..institutional.cloakbrowser_compat import ensure_cloakbrowser_platform_compatible
        ensure_cloakbrowser_platform_compatible()
    except Exception:
        try:
            from .institutional.cloakbrowser_compat import ensure_cloakbrowser_platform_compatible
            ensure_cloakbrowser_platform_compatible()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API — drop-in replacements for camofox.py functions
# ---------------------------------------------------------------------------

def is_available(config: dict[str, Any] | None = None) -> bool:
    """Check if CloakBrowser is available (importable)."""
    return _check_cloakbrowser()


# ---------------------------------------------------------------------------
# Tab registry — maps tab_id → (browser, context, page)
# Used by create_tab/evaluate_js/navigate_tab/close_tab for sequential
# tab-based workflows within a single operation (thread-safe: one thread per tab).
# ---------------------------------------------------------------------------

_tabs: dict[str, Any] = {}  # tab_id → page
_captured: dict[str, list] = {}  # tab_id → captured PDF responses


def _register_tab(browser, context, page) -> str:
    tab_id = uuid.uuid4().hex[:12]
    _tabs[tab_id] = page
    _captured[tab_id] = []

    # Listen for PDF responses
    def _on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            if "pdf" in ct or "octet-stream" in ct:
                try:
                    body = response.body()
                    if body[:5] == b"%PDF-":
                        _captured[tab_id].append({
                            "url": response.url,
                            "status": response.status,
                            "contentType": ct,
                            "dataBase64": base64.b64encode(body).decode(),
                        })
                except Exception:
                    pass
        except Exception:
            pass

    try:
        page.on("response", _on_response)
    except Exception:
        pass

    return tab_id


def _resolve_tab(tab_id: str):
    """Look up page for a tab_id. Returns page or None."""
    page = _tabs.get(tab_id)
    return page


def solve_url(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = 60000,
) -> dict[str, Any] | None:
    """Fetch URL via CloakBrowser shared browser. Returns dict with status/solution keys."""
    page = None
    try:
        _, context = _get_shared_browser(config)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=int(max_timeout))

        raw_cookies = context.cookies()
        cookies = [{"name": c["name"], "value": c.get("value", "")} for c in raw_cookies if c.get("name")]

        html = ""
        try:
            html = page.content()
        except Exception as e:
            logger.info(f"browser_engine: HTML extraction failed: {e}")

        final_url = page.url
        logger.info(f"browser_engine: ok, final_url={final_url}, html_len={len(html)}")
        return {
            "status": "ok",
            "solution": {
                "url": final_url,
                "status": 200,
                "response": html,
                "cookies": cookies,
            },
        }
    except Exception as e:
        logger.info(f"browser_engine: error - {e}")
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def get_cookies(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = 60000,
) -> dict[str, str] | None:
    """Solve and return cookies as a dict."""
    result = solve_url(url, config, max_timeout=max_timeout)
    if not result:
        return None
    solution = result.get("solution", {})
    cookies = solution.get("cookies", [])
    if isinstance(cookies, list):
        return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    return None


def get_html(
    url: str,
    config: dict[str, Any],
    *,
    max_timeout: int = 60000,
) -> str | None:
    """Solve and return page HTML."""
    result = solve_url(url, config, max_timeout=max_timeout)
    if not result:
        return None
    return result.get("solution", {}).get("response")


def import_cookies(cookie_file: str | Path, config: dict[str, Any], *, domain_suffix: str | None = None) -> int:
    """Import Netscape-format cookies into the shared browser context. Returns count imported."""
    try:
        text = Path(cookie_file).read_text(encoding="utf-8")
    except Exception as e:
        logger.info(f"browser_engine: failed to read cookie file: {e}")
        return 0
    cookies = _parse_netscape_cookies(text)
    if not cookies:
        return 0
    if domain_suffix:
        cookies = [c for c in cookies if domain_suffix in c.get("domain", "")]
    try:
        _, ctx = _get_shared_browser(config)
        ctx.add_cookies(cookies)
        logger.info(f"browser_engine: imported {len(cookies)} cookies")
        return len(cookies)
    except Exception as e:
        logger.info(f"browser_engine: import_cookies error: {e}")
        return 0


def evaluate_js(
    tab_id: str,
    expression: str,
    config: dict[str, Any],
    *,
    timeout: float = 15.0,
) -> Any:
    """Evaluate JavaScript expression in a browser page. Returns the JS result."""
    page = _resolve_tab(tab_id)
    if not page:
        logger.info(f"browser_engine: evaluate_js - tab {tab_id} not found")
        return None
    try:
        return page.evaluate(expression)
    except Exception as e:
        logger.info(f"browser_engine: evaluate_js error: {e}")
        return None


def create_tab(url: str, config: dict[str, Any], *, timeout: float = 30.0) -> str | None:
    """Create a new tab (page) in the shared browser and navigate to URL. Returns tab_id or None."""
    try:
        browser, context = _get_shared_browser(config)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        tab_id = _register_tab(browser, context, page)
        return tab_id
    except Exception as e:
        logger.info(f"browser_engine: create_tab failed - {e}")
        return None


def close_tab(tab_id: str, config: dict[str, Any]) -> None:
    """Close a browser tab (page only, not the shared browser)."""
    page = _resolve_tab(tab_id)
    if page:
        try:
            page.close()
        except Exception:
            pass
    _tabs.pop(tab_id, None)
    _captured.pop(tab_id, None)


def navigate_tab(tab_id: str, url: str, config: dict[str, Any], *, timeout: float = 30.0) -> bool:
    """Navigate an existing page to a new URL."""
    page = _resolve_tab(tab_id)
    if not page:
        logger.info(f"browser_engine: navigate_tab - tab {tab_id} not found")
        return False
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        return True
    except Exception as e:
        logger.info(f"browser_engine: navigate failed - {e}")
        return False


def get_snapshot(tab_id: str, config: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
    """Get page content as a snapshot dict."""
    page = _resolve_tab(tab_id)
    if not page:
        return {"url": "", "snapshot": "", "error": "tab not found"}
    try:
        html = page.content()
        url = page.url
        return {"url": url, "snapshot": html, "status": 200}
    except Exception as e:
        return {"url": "", "snapshot": "", "error": str(e)}


def download_pdf_via_browser(
    pdf_url: str,
    output_path: Path,
    config: dict[str, Any],
    *,
    timeout: float = 60.0,
) -> bool:
    """Download a PDF URL via CloakBrowser shared browser. Returns True on success.

    4-strategy cascade:
    1. Network response capture (from page.on("response"))
    2. In-browser fetch API with credentials
    3. PDF link discovery in page DOM
    4. Download button click
    """
    page = None
    try:
        _, context = _get_shared_browser(config)
        page = context.new_page()

        # Set up response listener for PDF captures
        captured_responses: list[dict] = []

        def _on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "pdf" in ct or "octet-stream" in ct:
                    try:
                        body = response.body()
                        if body[:5] == b"%PDF-":
                            captured_responses.append({
                                "url": response.url,
                                "dataBase64": base64.b64encode(body).decode(),
                            })
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            page.on("response", _on_response)
        except Exception:
            pass

        page.goto(pdf_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        time.sleep(3)

        # Check for anti-bot challenges
        html = ""
        try:
            html = page.content()
        except Exception:
            pass
        lower_html = html.lower()
        if any(sig in lower_html for sig in [
            "cf-browser-verification", "challenge-platform",
            "just a moment", "attention required",
            "security check", "captcha",
            "请稍候", "正在验证", "checking your browser",
            # ALTCHA anti-bot verification (used by sci-hub.ru and other Sci-Hub mirrors)
            "altcha", "你是机器人吗", "not a robot", "nope",
        ]):
            logger.info("browser_engine: anti-bot challenge detected, waiting...")
            time.sleep(10)

        current_url = page.url

        # Strategy 0: Network response capture
        for resp in captured_responses:
            data = resp.get("dataBase64", "")
            if data:
                pdf_bytes = base64.b64decode(data)
                if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(pdf_bytes)
                    logger.info(f"browser_engine: downloaded {len(pdf_bytes)} bytes via network capture")
                    return True

        # Build candidate fetch paths
        fetch_paths: list[str] = []
        parsed = urlparse(str(current_url))
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Extract DOI from URL for pdfdirect construction
        doi_from_url = None
        path = parsed.path
        for prefix in ["/doi/", "/doi/pdf/", "/doi/pdfdirect/", "/doi/epdf/", "/articles/"]:
            if prefix in path:
                doi_from_url = path.split(prefix)[-1].split("?")[0].split("#")[0]
                break

        if doi_from_url:
            fetch_paths.append(f"/doi/pdfdirect/{doi_from_url}")
            fetch_paths.append(f"/doi/pdf/{doi_from_url}")
            fetch_paths.append(f"/content/pdf/{doi_from_url}.pdf")

        if _is_pdf_url(pdf_url):
            parsed_orig = urlparse(pdf_url)
            if parsed_orig.netloc == parsed.netloc:
                fetch_paths.append(parsed_orig.path)

        # Strategy 1: In-browser fetch API
        for fetch_path in fetch_paths:
            logger.info(f"browser_engine: trying in-browser fetch {origin}{fetch_path[:60]}")
            try:
                pdf_b64 = page.evaluate(f"""
                    (async () => {{
                        try {{
                            const resp = await fetch('{fetch_path}', {{
                                credentials: 'include',
                                headers: {{'Accept': 'application/pdf,*/*'}}
                            }});
                            if (!resp.ok) return 'status:' + resp.status;
                            const ct = resp.headers.get('content-type') || '';
                            if (!ct.includes('pdf') && !ct.includes('octet')) return 'ct:' + ct;
                            const blob = await resp.blob();
                            return new Promise((resolve) => {{
                                const reader = new FileReader();
                                reader.onload = () => resolve(reader.result);
                                reader.readAsDataURL(blob);
                            }});
                        }} catch(e) {{
                            return 'error:' + e.message;
                        }}
                    }})()
                """)

                if isinstance(pdf_b64, str) and pdf_b64.startswith("data:"):
                    header, data = pdf_b64.split(",", 1)
                    pdf_bytes = base64.b64decode(data)
                    if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(pdf_bytes)
                        logger.info(f"browser_engine: downloaded {len(pdf_bytes)} bytes via in-browser fetch")
                        return True
                    else:
                        logger.info(f"browser_engine: fetch returned non-PDF ({len(pdf_bytes)} bytes)")
                else:
                    logger.info(f"browser_engine: fetch result: {str(pdf_b64)[:80]}")
            except Exception as e:
                logger.info(f"browser_engine: fetch error: {e}")

        # Strategy 2: PDF link discovery in DOM
        try:
            pdf_link = page.evaluate("""
                (() => {
                    for (const el of document.querySelectorAll('iframe, embed, object')) {
                        const src = el.src || el.data || '';
                        if (src.includes('.pdf') && !src.includes('supplement') && !src.includes('Suppl')) return src;
                    }
                    const viewer = document.querySelector('#viewer, .pdfViewer, [data-l10n-id="download"]');
                    if (viewer) return window.location.href;
                    for (const a of document.querySelectorAll('a[href]')) {
                        const href = (a.href || '').toLowerCase();
                        const text = (a.innerText || '').toLowerCase();
                        if (href.includes('supplement') || href.includes('supporting') || href.includes('downloadsupplement') || href.includes('pb-assets')) continue;
                        if (text.includes('supplement') || text.includes('supporting info')) continue;
                        if (href.includes('.pdf') || href.includes('/pdf/') || href.includes('pdfdirect')) {
                            if (a.href.startsWith('http')) return a.href;
                        }
                    }
                    const sdPdf = document.querySelector('a[aria-label*="PDF"], a[aria-label*="pdf"], a.pdf-download-btn-link');
                    if (sdPdf) return sdPdf.href;
                    return null;
                })()
            """)

            if pdf_link and isinstance(pdf_link, str) and pdf_link.startswith("http"):
                logger.info(f"browser_engine: found PDF link: {pdf_link[:80]}")
                parsed_link = urlparse(pdf_link)
                link_path = parsed_link.path + ("?" + parsed_link.query if parsed_link.query else "")
                if parsed_link.netloc == parsed.netloc:
                    try:
                        pdf_b64 = page.evaluate(f"""
                            (async () => {{
                                try {{
                                    const resp = await fetch('{link_path}', {{
                                        credentials: 'include',
                                        headers: {{'Accept': 'application/pdf,*/*'}}
                                    }});
                                    if (!resp.ok) return 'status:' + resp.status;
                                    const ct = resp.headers.get('content-type') || '';
                                    if (!ct.includes('pdf') && !ct.includes('octet')) return 'ct:' + ct;
                                    const blob = await resp.blob();
                                    return new Promise((resolve) => {{
                                        const reader = new FileReader();
                                        reader.onload = () => resolve(reader.result);
                                        reader.readAsDataURL(blob);
                                    }});
                                }} catch(e) {{
                                    return 'error:' + e.message;
                                }}
                            }})()
                        """)

                        if isinstance(pdf_b64, str) and pdf_b64.startswith("data:"):
                            header, data = pdf_b64.split(",", 1)
                            pdf_bytes = base64.b64decode(data)
                            if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                                output_path.parent.mkdir(parents=True, exist_ok=True)
                                output_path.write_bytes(pdf_bytes)
                                logger.info(f"browser_engine: downloaded {len(pdf_bytes)} bytes via PDF link fetch")
                                return True
                    except Exception as e:
                        logger.info(f"browser_engine: PDF link fetch error: {e}")
        except Exception as e:
            logger.info(f"browser_engine: DOM scan error: {e}")

        # Strategy 3: Click download button
        try:
            clicked = page.evaluate("""
                (() => {
                    for (const btn of document.querySelectorAll(
                        '#download, [aria-label*="download" i], [aria-label*="PDF" i], .pdf-download-btn-link, a[data-aa-name="download-pdf"]'
                    )) {
                        if (btn.offsetParent !== null) { btn.click(); return true; }
                    }
                    return false;
                })()
            """)
            if clicked:
                logger.info("browser_engine: clicked download button, waiting...")
                time.sleep(5)
        except Exception as e:
            logger.info(f"browser_engine: click error: {e}")

        return False
    except Exception as e:
        logger.info(f"browser_engine: download_pdf_via_browser error: {e}")
        return False
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


# Backward-compat alias
download_pdf_via_camofox = download_pdf_via_browser


def _is_pdf_url(url: str) -> bool:
    """Check if a URL looks like a direct PDF link."""
    lower = url.lower()
    return (
        lower.endswith(".pdf")
        or "/pdf/" in lower
        or "content/pdf" in lower
        or "pdfdirect" in lower
        or "/doi/pdf/" in lower
        or "type=printable" in lower
    )


def fetch_url(
    tab_id: str,
    url: str,
    config: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    """Navigate tab to URL and capture any PDF responses from the network layer."""
    page = _resolve_tab(tab_id)
    if not page:
        logger.info(f"browser_engine: fetch_url - tab {tab_id} not found")
        return None

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
    except Exception as e:
        logger.info(f"browser_engine: fetch_url navigate error: {e}")

    # Check captured responses
    for resp in _captured.get(tab_id, []):
        data = resp.get("dataBase64", "")
        if data:
            pdf_bytes = base64.b64decode(data)
            if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                return {"status": "ok", "bytes": len(pdf_bytes), "data": pdf_bytes}

    # Also check direct response body
    try:
        response = page.goto(url, wait_until="commit", timeout=int(timeout * 1000))
        if response is not None:
            body = response.body()
            if body[:5] == b"%PDF-" and len(body) > 5000:
                return {"status": "ok", "bytes": len(body), "data": body}
    except Exception:
        pass

    return None


def get_captured_responses(
    tab_id: str,
    config: dict[str, Any],
    *,
    consume: bool = True,
) -> list[dict[str, Any]]:
    """Get captured PDF responses for a tab. Optionally consume (clear) them."""
    captured = _captured.get(tab_id, [])
    result = list(captured)
    if consume:
        _captured[tab_id] = []
    return result


def close_all_tabs(config: dict[str, Any]) -> None:
    """Close all tracked browser tabs (pages only, not the shared browser)."""
    for tab_id in list(_tabs.keys()):
        close_tab(tab_id, config)


# ---------------------------------------------------------------------------
# Netscape cookie parser (shared utility)
# ---------------------------------------------------------------------------

def _parse_netscape_cookies(text: str) -> list[dict[str, Any]]:
    """Parse Netscape-format cookie file into structured cookie objects."""
    cookies: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line.removeprefix("#HttpOnly_")
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0]
        cookie_path = parts[2]
        secure = parts[3].upper() == "TRUE"
        try:
            expires = int(parts[4])
        except (ValueError, IndexError):
            expires = 0
        name = parts[5]
        value = "\t".join(parts[6:])
        cookie: dict[str, Any] = {
            "name": name, "value": value,
            "domain": domain, "path": cookie_path,
            "secure": secure, "expires": expires,
        }
        if http_only:
            cookie["httpOnly"] = True
        cookies.append(cookie)
    return cookies
