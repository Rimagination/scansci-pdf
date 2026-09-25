"""EZProxy institutional proxy source.

Uses the university library's EZProxy service to access papers.
EZProxy rewrites URLs through the library proxy, providing
institutional access to subscribed journals.

Two deployment styles are handled:

* **prefix style** — the configured template carries ``{url}``
  (``https://libproxy.example.edu/login?url={url}``).
* **hostname-rewriting style** — the template has no ``{url}`` placeholder; the host
  of the configured base is then the rewriting suffix and the target host is dashed
  into it (``pubs.acs.org`` -> ``pubs-acs-org.lib.ezproxy.hkust.edu.hk``). Verified
  against HKUST, where the prefix forms 302 straight back to the raw publisher and
  therefore never proxy anything.

Fetching order: plain HTTP with the saved session cookies (direct download shapes
first, then PDF links scraped from the landing page), falling back to a browser for
JS-only publishers and interactive login.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

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

_HTTP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# Link shapes that lead to a downloadable PDF, used when filtering scraped hrefs.
_PDF_LINK_TOKENS = ("pdfdirect", ".pdf", "articlepdf", "content/pdf", "type=printable", "pdfplus")

# Direct download paths that beat scraping, because publisher landing pages usually
# expose the in-page viewer (/doi/pdf/, which returns HTML) rather than the download
# endpoint. {doi} is the bare DOI. Verified against live sessions.
_DIRECT_PDF_PATHS: dict[str, str] = {
    "10.1002": "/doi/pdfdirect/{doi}?download=true",  # Wiley
    "10.1021": "/doi/pdf/{doi}",                      # ACS
}

# Fields Chromium's Storage.setCookies accepts, and the sameSite spellings it knows.
_ALLOWED_COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
_SAMESITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "",
    "": "",
}


def _get_ezproxy_base(config: dict[str, Any]) -> str:
    """Get EZProxy login URL template."""
    return config.get("ezproxy_login_url", "")


def _rewrite_suffix(base: str) -> str:
    """Hostname-rewriting suffix implied by an EZProxy base URL.

    ``https://lib.ezproxy.hkust.edu.hk/login``       -> ``.lib.ezproxy.hkust.edu.hk``
    ``https://login.lib.ezproxy.hkust.edu.hk/login`` -> ``.lib.ezproxy.hkust.edu.hk``
    """
    host = urlparse(base).netloc
    if not host:
        return ""
    if host.startswith("login."):
        host = host[len("login."):]
    return "." + host


def _make_ezproxy_url(target_url: str, config: dict[str, Any]) -> str:
    """Convert a target URL to an EZProxy-proxied URL.

    With a ``{url}`` template the target is substituted into it (prefix style).
    Without one, the base's host is treated as the rewriting suffix and the target
    host is dashed into it (hostname-rewriting style). ``ezproxy_rewrite_suffix``
    overrides the derived suffix for unusual deployments.
    """
    base = _get_ezproxy_base(config)
    if not base:
        return ""
    if "{url}" in base:
        return base.replace("{url}", target_url)

    suffix = config.get("ezproxy_rewrite_suffix") or _rewrite_suffix(base)
    if not suffix:
        return ""

    parsed = urlparse(target_url)
    host = parsed.netloc
    if not host:
        return ""
    if host.endswith(suffix):
        return target_url
    return urlunparse(parsed._replace(netloc=f"{host.replace('.', '-')}{suffix}"))


def _direct_pdf_urls(doi: str, proxied_landing: str) -> list[str]:
    """Known-good direct download URLs for this DOI, on the given proxied host."""
    template = _DIRECT_PDF_PATHS.get(doi.split("/", 1)[0])
    if not template:
        return []
    parsed = urlparse(proxied_landing)
    if not parsed.netloc:
        return []
    path, _, query = template.format(doi=doi).partition("?")
    return [urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))]


def _rank_pdf_url(url: str) -> int:
    """Prefer direct-download shapes over in-page viewers / landing pages."""
    low = url.lower()
    if "pdfdirect" in low:
        return 0
    if low.endswith(".pdf") or ".pdf?" in low:
        return 1
    if "articlepdf" in low or "content/pdf" in low:
        return 2
    return 3


def _load_session_cookies(config: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read the saved EZProxy cookie jar, or None when there is nothing usable."""
    cookie_file = _ezproxy_cookie_path(config)
    if not cookie_file.exists():
        return None
    try:
        import json
        cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    return cookies or None


def _has_ezproxy_session_cookie(cookies: list[dict[str, Any]]) -> bool:
    """True when the jar carries an actual EZProxy session cookie.

    A browser login that never authenticated still saves a jar (guest cookies from
    the publisher), so the cookie *names* — not the file's existence — are the signal.
    """
    return any("ezproxy" in (c.get("name") or "").lower() for c in cookies)


def _sanitize_playwright_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop/normalise cookie fields Chromium rejects with "Invalid cookie fields".

    A jar dumped by one browser can carry attributes another refuses — e.g. sameSite
    "unspecified" / "no_restriction", or sameSite None without ``secure`` (a real
    combination in SSO cookies). Keep only documented fields and satisfy that rule.
    """
    clean: list[dict[str, Any]] = []
    for c in cookies:
        if not c.get("name") or not c.get("domain"):
            continue
        out = {k: c[k] for k in _ALLOWED_COOKIE_FIELDS if k in c}
        same = _SAMESITE_MAP.get(str(out.get("sameSite", "")).lower())
        if same:
            out["sameSite"] = same
            if same == "None" and not out.get("secure"):
                out["secure"] = True
        else:
            out.pop("sameSite", None)
        if isinstance(out.get("expires"), (int, float)) and out["expires"] <= 0:
            out.pop("expires", None)
        clean.append(out)
    return clean


def _validate_ezproxy_session(config: dict[str, Any]) -> bool:
    """Check if saved EZProxy cookies still work."""
    cookies = _load_session_cookies(config)
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
        resp = sess.get(test_url, timeout=20, allow_redirects=True)
    except Exception:
        return False
    final = resp.url.lower()
    # Redirected back to the proxy's own login endpoint => the session is dead.
    if "libproxy" in final or "/login" in final:
        return False
    if resp.status_code == 200:
        return True
    # Publishers routinely answer 401/403 for a single probe path (or for a title we
    # do not subscribe to) while the session itself is fine: trust a real session
    # cookie instead of declaring a valid session expired.
    return resp.status_code in (401, 403) and _has_ezproxy_session_cookie(cookies)


def _ezproxy_cookie_path(config: dict[str, Any]) -> Path:
    """Get path to saved EZProxy cookies."""
    from ..config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    return cache_dir / "ezproxy_cookies.json"


def _try_ezproxy_http(
    ezproxy_url: str, doi: str, output_path: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Fetch through the proxy over plain HTTP using the saved session cookies.

    Order: known direct download URLs, then PDF-looking links scraped from the
    landing page (``citation_pdf_url`` first, then hrefs); the first response that is
    really a PDF wins. Returns None so the caller can fall back to the browser.
    Headless and much faster than driving a browser.
    """
    cookies = _load_session_cookies(config)
    if not cookies:
        return None
    if not _has_ezproxy_session_cookie(cookies):
        log.info("   [EZProxy] saved cookies carry no EZProxy session cookie - login required")
        return None

    sess = requests.Session()
    sess.trust_env = False
    for c in cookies:
        sess.cookies.set(c["name"], c.get("value", ""), domain=c.get("domain", ""), path=c.get("path", "/"))
    sess.headers.update({"User-Agent": _HTTP_UA})

    candidates: list[str] = _direct_pdf_urls(doi, _make_ezproxy_url(ezproxy_url, config) or ezproxy_url)

    try:
        resp = sess.get(ezproxy_url, timeout=60, allow_redirects=True)
    except Exception as exc:
        log.info(f"   [EZProxy] http attempt failed: {exc}")
        resp = None

    if resp is not None:
        if _response_looks_pdf(resp, resp.content[:5]):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(resp.content)
            return success(doi, output_path, "EZProxy")

        html = resp.content[:400000].decode("utf-8", "ignore")
        picked = extract_pdf_url_from_html(html, resp.url)
        if picked:
            candidates.append(picked)
        for href in re.findall(r"""href=["']([^"']+)["']""", html, re.I):
            if any(tok in href.lower() for tok in _PDF_LINK_TOKENS):
                candidates.append(urljoin(resp.url, href))

    for cand in sorted(dict.fromkeys(candidates), key=_rank_pdf_url)[:6]:
        try:
            sub = sess.get(cand, timeout=60, allow_redirects=True)
        except Exception:
            continue
        if _response_looks_pdf(sub, sub.content[:5]):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(sub.content)
            return success(doi, output_path, "EZProxy")
        log.info(f"   [EZProxy] link is not a PDF: {cand[:90]} ({sub.status_code})")
    return None


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

    First over plain HTTP with the saved session cookies; if that yields nothing,
    drive a browser (which also handles JS-only publishers and interactive login).
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

    # Fast path: plain HTTP + saved cookies (no browser, works headless).
    http_result = _try_ezproxy_http(ezproxy_url, doi, output_path, config)
    if http_result:
        log.info("   [EZProxy] got the PDF over HTTP with the saved session")
        return http_result

    try:
        from ..browser_backend import launch
        from ..browser_backend import is_available as _browser_backend_available
        from ..browser_engine import _build_browser_args
        if not _browser_backend_available():
            log.info("   [EZProxy] no browser backend available")
            return None
    except ImportError:
        log.info("   [EZProxy] no browser backend available")
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

    browser = launch(headless=bool(config.get("ezproxy_headless", False)), humanize=True, args=args)
    try:
        context = browser.new_context()
        page = context.new_page()
        page.on("response", _on_response)

        # Load saved cookies if available (sanitised: Chromium rejects some fields)
        cookies = _load_session_cookies(config)
        if cookies:
            context.add_cookies(_sanitize_playwright_cookies(cookies))

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

        # Look for PDF links in page (ranked: direct download shapes first)
        links = page.evaluate("""() => {
            const out = [];
            for (const link of document.querySelectorAll('a')) {
                const href = link.href || '';
                const text = (link.innerText || '').toLowerCase();
                if (!href) continue;
                const low = href.toLowerCase();
                if (low.includes('pdf') || text.includes('pdf')) {
                    if (!text.includes('purchase') && !low.includes('purchase')) out.push(href);
                }
            }
            return out;
        }""") or []
        ordered = sorted(dict.fromkeys(links), key=_rank_pdf_url)[:3]
        for pdf_link in ordered:
            log.info(f"   [EZProxy] Found PDF link: {pdf_link[:80]}")
            captured_pdf.clear()
            try:
                page.goto(pdf_link, wait_until="commit", timeout=30000)
            except Exception:
                continue
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
