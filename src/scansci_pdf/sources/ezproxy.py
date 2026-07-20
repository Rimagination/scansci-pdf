"""EZProxy institutional proxy source.

Uses the university library's EZProxy service to access papers.
EZProxy rewrites URLs through the library proxy, providing
institutional access to subscribed journals.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from ..log import get_logger
from ..pdf_utils import success
from ..private_files import atomic_write_private
from ..publisher_pdf_resolver import PublisherPdfResolver

log = get_logger()
_PUBLISHER_PDF_RESOLVER = PublisherPdfResolver()

_CHALLENGE_MARKERS = (
    "processing verification",
    "security verification",
    "verify you are human",
    "checking your browser",
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "challenge-platform",
    "crasolve",
    "captcha",
    "are you a robot",
    "not a robot",
    "verification required",
    "complete the captcha",
    "robot check",
    "press and hold",
    "cf-turnstile",
    "g-recaptcha",
    "hcaptcha",
    "datadome",
    "perimeterx",
)

_LOGIN_URL_MARKERS = (
    "/login",
    "/signin",
    "/authenticate",
    "cas/login",
    "shibboleth",
    "saml",
    "openathens",
    "wayf",
)

_LOGIN_TEXT_MARKERS = (
    "institutional login",
    "sign in through your institution",
    "access through your institution",
    "find your institution",
    "choose your institution",
)

_FETCH_PDF_JS = r"""
async (url) => {
  try {
    const response = await fetch(url, {credentials: 'include'});
    if (!response.ok) return '';
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.length < 5 || bytes[0] !== 0x25 || bytes[1] !== 0x50 ||
        bytes[2] !== 0x44 || bytes[3] !== 0x46 || bytes[4] !== 0x2d) return '';
    const chunk = 0x8000;
    let binary = '';
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  } catch (_) {
    return '';
  }
}
""".strip()


def _page_snapshot(page: Any) -> tuple[str, str, str]:
    """Best-effort URL, title and HTML snapshot of a live browser page."""
    values: list[str] = []
    for getter in (lambda: page.url, page.title, page.content):
        try:
            values.append(str(getter()))
        except Exception:
            values.append("")
    return values[0], values[1], values[2]


def _page_is_challenge(page: Any) -> bool:
    url, title, html = _page_snapshot(page)
    haystack = "\n".join((url, title, html)).lower()[:50000]
    return any(marker in haystack for marker in _CHALLENGE_MARKERS)


def _page_is_login(page: Any) -> bool:
    url, title, html = _page_snapshot(page)
    lowered_url = url.lower()
    lowered_page = f"{title}\n{html}".lower()[:50000]
    return any(marker in lowered_url for marker in _LOGIN_URL_MARKERS) or any(
        marker in lowered_page for marker in _LOGIN_TEXT_MARKERS
    )


def _redacted_url(url: str) -> str:
    """Return a log-safe URL without signed query parameters or fragments."""
    try:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return "(unavailable)"


def _continue_after_timeout(config: dict[str, Any], phase: str) -> bool:
    """Offer another wait window only for an explicitly interactive CLI call."""
    if not config.get("_ezproxy_interactive", False) or not sys.stdin.isatty():
        log.info(f"   [EZProxy] {phase} timed out; non-interactive download failed.")
        return False
    try:
        answer = input(
            f"  EZProxy {phase} 仍未完成。直接按 Enter 继续等待，或输入 skip 退出: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer != "skip"


def _discover_pdf_link(page: Any) -> str:
    return _PUBLISHER_PDF_RESOLVER.resolve(page)


def _latest_context_page(page: Any) -> Any:
    """Follow publisher controls that open a new tab in the same context."""
    try:
        pages = list(page.context.pages)
    except Exception:
        return page
    for candidate in reversed(pages):
        try:
            if not candidate.is_closed():
                return candidate
        except AttributeError:
            return candidate
        except Exception:
            continue
    return page


def _navigate(page: Any, url: str, *, wait_until: str) -> None:
    """Start navigation while treating a browser timeout as recoverable."""
    try:
        page.goto(url, wait_until=wait_until, timeout=30000)
    except Exception as exc:
        if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
            log.info(
                "   [EZProxy] Navigation is still progressing; "
                "continuing to watch the open browser..."
            )
            return
        raise


def _captured_pdf(captured_pdf: list[bytes]) -> bytes | None:
    return next(
        (data for data in reversed(captured_pdf) if len(data) > 5000 and data[:5] == b"%PDF-"),
        None,
    )


def _fetch_pdf_in_page(page: Any) -> bytes | None:
    """Fetch the current PDF URL with the page's authenticated browser context."""
    try:
        encoded = page.evaluate(_FETCH_PDF_JS, page.url)
        if not isinstance(encoded, str) or not encoded:
            return None
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 5000 and data[:5] == b"%PDF-":
            return data
    except Exception:
        return None
    return None


def _wait_for_pdf_link(
    page: Any,
    captured_pdf: list[bytes],
    config: dict[str, Any],
) -> str:
    """Poll article/login/challenge states until a PDF entrypoint appears."""
    timeout = int(config.get("ezproxy_challenge_timeout", 120))
    challenge_logged = False
    login_logged = False
    while True:
        for _ in range(max(1, timeout // 2)):
            page = _latest_context_page(page)
            if _captured_pdf(captured_pdf):
                return page.url

            if _page_is_challenge(page):
                if not challenge_logged:
                    log.info(
                        "   [EZProxy] Verification detected on article page; "
                        "complete it in the open browser..."
                    )
                    challenge_logged = True
            else:
                pdf_link = _discover_pdf_link(page)
                if pdf_link:
                    return pdf_link
                if _page_is_login(page) and not login_logged:
                    log.info("   [EZProxy] Login required; complete it in the open browser...")
                    login_logged = True
            time.sleep(2)

        if not _continue_after_timeout(config, "article/login verification"):
            return ""


def _wait_for_pdf_bytes(
    page: Any,
    captured_pdf: list[bytes],
    config: dict[str, Any],
) -> bytes | None:
    """Poll the PDF/challenge page until browser response or in-page fetch succeeds."""
    timeout = int(config.get("ezproxy_challenge_timeout", 120))
    challenge_logged = False
    while True:
        for _ in range(max(1, timeout // 2)):
            page = _latest_context_page(page)
            captured = _captured_pdf(captured_pdf)
            if captured:
                return captured

            if _page_is_challenge(page):
                if not challenge_logged:
                    log.info(
                        "   [EZProxy] Verification detected on PDF page; "
                        "complete it in the open browser..."
                    )
                    challenge_logged = True
            else:
                fetched = _fetch_pdf_in_page(page)
                if fetched:
                    return fetched
            time.sleep(2)

        if not _continue_after_timeout(config, "PDF verification"):
            return None


def _save_context_cookies(context: Any, cookie_file: Path) -> None:
    """Atomically persist refreshed cookies with owner-only permissions."""
    try:
        cookies = context.cookies()
        if not cookies:
            return
        atomic_write_private(
            cookie_file,
            json.dumps(cookies, ensure_ascii=False, indent=2),
        )
    except Exception as exc:
        log.info(f"   [EZProxy] Could not refresh cookie cache: {type(exc).__name__}")


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

    args = _build_browser_args(config)
    captured_pdf: list[bytes] = []

    def _on_response(response):
        try:
            ct = response.headers.get("content-type", "").lower()
            if "pdf" in ct or "octet-stream" in ct:
                try:
                    body = response.body()
                    if body and len(body) > 5000 and body[:5] == b"%PDF-":
                        captured_pdf.append(body)
                except Exception:
                    pass
        except Exception:
            pass

    browser = None
    context = None
    cookie_file = _ezproxy_cookie_path(config)
    try:
        browser = launch(headless=False, humanize=True, args=args)
        context = browser.new_context()
        page = context.new_page()
        page.on("response", _on_response)
        try:
            context.on("page", lambda opened_page: opened_page.on("response", _on_response))
        except Exception:
            pass

        # Load saved cookies if available
        if cookie_file.exists():
            try:
                cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
                if cookies:
                    context.add_cookies(cookies)
            except Exception as exc:
                log.info(f"   [EZProxy] Cookie cache could not be loaded: {type(exc).__name__}")

        # Navigate to EZProxy URL
        _navigate(page, ezproxy_url, wait_until="domcontentloaded")
        pdf_link = _wait_for_pdf_link(page, captured_pdf, config)

        pdf_bytes = _captured_pdf(captured_pdf)
        if not pdf_bytes and pdf_link:
            log.info(f"   [EZProxy] Found PDF entry: {_redacted_url(pdf_link)}")
            captured_pdf.clear()
            _navigate(page, pdf_link, wait_until="commit")
            pdf_bytes = _wait_for_pdf_bytes(page, captured_pdf, config)

        if pdf_bytes:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(pdf_bytes)
            return success(doi, output_path, "EZProxy")

    except Exception as e:
        log.info(f"   [EZProxy] Error: {type(e).__name__}")
    finally:
        if context is not None:
            _save_context_cookies(context, cookie_file)
        try:
            if browser is not None:
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
