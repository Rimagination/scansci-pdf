"""Supplementary Information (SI) discovery and download.

v2: lenient publishers over plain HTTP; Cloudflare-protected ones (Elsevier's
linkinghub bounce) fall back to the stealth-browser backend, reusing its
cookies for the attachment downloads. SI failure never fails the main download.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

_LINK_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|gz|csv|txt)(?:[?#]|$)", re.I)
_MARKER_RE = re.compile(r"(suppl|supp_|supporting|_si\d|/cms/|attachment|mmc\d|moesm)", re.I)
# URLs that scream "attachment" even without a file extension (PLOS-style
# .../file?id=<doi>.s001&type=supplementary-file) — the download step validates
# by content-type instead of trusting the URL. MOESM files carry extensions,
# so they ride the weak rule; page anchors (#MOESM1) stay excluded.
_STRONG_RE = re.compile(r"(supplementary[-_]?file|/cms/|attachment|mmc\d)", re.I)
_MAIN_PDF_RE = re.compile(r"(?:^|/)(?:main|mainext|article)\.pdf(?:[?#]|$)", re.I)

_CT_EXT = (
    ("application/pdf", ".pdf"),
    ("wordprocessingml.document", ".docx"),
    ("msword", ".doc"),
    ("spreadsheetml.sheet", ".xlsx"),
    ("vnd.ms-excel", ".xls"),
    ("presentationml.presentation", ".pptx"),
    ("text/csv", ".csv"),
    ("application/zip", ".zip"),
    ("application/gzip", ".gz"),
    ("text/plain", ".txt"),
)


def extract_supplementary_links(html: str, base_url: str, max_files: int = 10) -> list[str]:
    """Return absolute candidate SI URLs found on an article landing page."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _LINK_RE.finditer(html or ""):
        href = m.group(1)
        if not _MARKER_RE.search(href):
            continue
        if _MAIN_PDF_RE.search(href):
            continue  # the article's own main PDF, not an attachment
        # Weak marker + known extension is enough; strong markers (PLOS-style
        # supplementary-file URLs) qualify even without an extension.
        if not _EXT_RE.search(href) and not _STRONG_RE.search(href):
            continue
        url = urljoin(base_url or "", href)
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= max_files:
            break
    return out


def fetch_supplementary(
    doi: str,
    output_dir: str | Path,
    config: dict | None = None,
    max_files: int = 10,
    timeout: int = 30,
) -> list[str]:
    """Download SI attachments for ``doi``; returns saved file paths.

    Never raises: problems are reported by returning fewer/zero files.
    """
    config = config or {}
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    article_url = _article_url(doi)
    if not article_url:
        return []

    html, fetch_bytes = _fetch_page(article_url, config, timeout)
    if not html:
        return []
    links = extract_supplementary_links(html, article_url, max_files=max_files)

    saved: list[str] = []
    stem = _safe_stem(doi)
    for i, url in enumerate(links, 1):
        body, ctype = fetch_bytes(url)
        if not body:
            continue
        ctype = (ctype or "").lower()
        if "text/html" in ctype:
            continue  # another landing page, not an attachment
        ext = _ext_of(url) or _ext_from_content_type(ctype) or ".bin"
        path = out / f"{stem}_SI{i}{ext}"
        path.write_bytes(body)
        saved.append(str(path))
    return saved


def _fetch_page(article_url: str, config: dict, timeout: int):
    """Get (html, fetch_bytes) for the article page.

    Plain HTTP first. When the publisher bounces us (Elsevier's linkinghub) or
    returns a non-200, retry through the stealth-browser backend so Cloudflare
    cookies are shared with the attachment downloads. fetch_bytes(url) returns
    (body | None, content-type | None).
    """
    import requests

    proxy = (config or {}).get("network_proxy", "")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": _user_agent()}

    session = requests.Session()
    session.trust_env = False
    blocked = False
    html = ""
    try:
        resp = session.get(article_url, headers=headers, proxies=proxies, timeout=timeout, allow_redirects=True)
        blocked = resp.status_code != 200 or "linkinghub." in str(resp.url)
        if resp.status_code == 200:
            html = resp.text
    except Exception:
        blocked = True

    if not blocked:
        def plain_fetch(url: str):
            try:
                r = session.get(url, headers=headers, proxies=proxies, timeout=timeout, allow_redirects=True)
                return (r.content, r.headers.get("content-type")) if r.status_code == 200 else (None, None)
            except Exception:
                return None, None
        return html, plain_fetch

    fetched = _browser_page(article_url, config, timeout)
    if fetched:
        return fetched
    return (html, None) if html else (None, None)


def _browser_page(article_url: str, config: dict, timeout: int):
    """Render the article page through the shared stealth-browser backend.

    Uses the browser-worker pool (browser_engine) instead of a cold start per
    call, and downloads attachments through the browser context's request API
    so Cloudflare cookies are shared. Returns (page_html, fetch_bytes); only
    the PAGE is closed - the pooled browser stays alive for the next paper.
    """
    try:
        from .browser_engine import get_browser_page

        page = get_browser_page(config)
    except Exception:
        return None
    if page is None:
        return None

    try:
        page.goto(article_url, wait_until="domcontentloaded", timeout=max(timeout, 45) * 1000)
        page.wait_for_timeout(2500)
        html = page.content()

        def fetch_bytes(url: str):
            try:
                r = page.context.request.get(url, timeout=max(timeout, 45) * 1000)
                if r.status != 200:
                    return None, None
                return r.body(), r.headers.get("content-type")
            except Exception:
                return None, None

        return html, fetch_bytes
    except Exception:
        try:
            ctx.close()
        except Exception:
            pass
        return None


def _ext_from_content_type(ctype: str) -> str | None:
    for frag, ext in _CT_EXT:
        if frag in ctype:
            return ext
    return None


def _article_url(doi: str) -> str | None:
    try:
        from .publisher_strategies import StrategyRegistry
        strategy = StrategyRegistry.get_for_doi(doi)
    except Exception:
        return None
    if strategy is None:
        return None
    try:
        return strategy.article_url(doi)
    except Exception:
        return None


def _ext_of(url: str) -> str | None:
    m = _EXT_RE.search(url)
    return f".{m.group(1).lower()}" if m else None


def _safe_stem(identifier: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", identifier.strip()).strip("_")


def _user_agent() -> str:
    try:
        from .network import USER_AGENT
        return USER_AGENT
    except Exception:  # pragma: no cover
        return "Mozilla/5.0 (compatible; scansci-pdf)"
