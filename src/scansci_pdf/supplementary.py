"""Supplementary Information (SI) discovery and download.

v1 strategy: resolve the article landing page via the publisher strategy
registry, scrape links that look like supplementary material, download them
next to the main PDF. SI failure never fails the main download.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

_LINK_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|gz|csv|txt)(?:[?#]|$)", re.I)
_MARKER_RE = re.compile(r"(suppl|supp_|supporting|_si\d|/cms/|attachment|mmc\d)", re.I)
_MAIN_PDF_RE = re.compile(r"(?:^|/)(?:main|mainext|article)\.pdf(?:[?#]|$)", re.I)


def extract_supplementary_links(html: str, base_url: str, max_files: int = 10) -> list[str]:
    """Return absolute candidate SI URLs found on an article landing page."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _LINK_RE.finditer(html or ""):
        href = m.group(1)
        if not _EXT_RE.search(href):
            continue
        if not _MARKER_RE.search(href):
            continue
        if _MAIN_PDF_RE.search(href):
            continue  # the article's own main PDF, not an attachment
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
    import requests

    config = config or {}
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    article_url = _article_url(doi)
    if not article_url:
        return []

    proxy = config.get("network_proxy", "")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": _user_agent()}
    try:
        s = requests.Session()
        s.trust_env = False
        resp = s.get(article_url, headers=headers, proxies=proxies, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return []
        links = extract_supplementary_links(resp.text, str(resp.url), max_files=max_files)
    except Exception:
        return []

    saved: list[str] = []
    stem = _safe_stem(doi)
    for i, url in enumerate(links, 1):
        try:
            r = s.get(url, headers=headers, proxies=proxies, timeout=timeout, allow_redirects=True)
            if r.status_code != 200 or not r.content:
                continue
            ctype = (r.headers.get("content-type") or "").lower()
            if "text/html" in ctype and not url.lower().endswith((".pdf", ".zip")):
                continue  # landing page again, not an attachment
            ext = _ext_of(url) or ".bin"
            path = out / f"{stem}_SI{i}{ext}"
            path.write_bytes(r.content)
            saved.append(str(path))
        except Exception:
            continue
    return saved


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
