"""Chinese journal official-site direct PDF lane.

Many Chinese journals publish free PDFs on their own websites (Magtech,
``downloadArticleFile.do?attachType=PDF``, ``/CN/article/...`` etc.).  Their
DOIs resolve straight to the journal site — no aggregator subscription is
needed.  This source follows the DOI landing page and scrapes the PDF link.

Aggregator landings (CNKI / Wanfang / VIP / chndoi) are deliberately skipped:
those are handled by their own lanes and would only produce paywall HTML here.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..chinese import normalize_doi_loose, resolve_chinese_doi, response_text
from ..log import get_logger
from ..network import CHROME_UA, proxy_dict, request_timeout, select_proxy_for_url
from ..pdf_utils import is_pdf_file, success

log = get_logger()

_AGGREGATOR_HOSTS = (
    "cnki.net", "cnki.com.cn", "chndoi.org", "wanfangdata.com.cn",
    "cqvip.com", "ncpssd.cn", "ncpssd.org",
)

_PDF_MARKERS = (
    "attachtype=pdf", "downloadarticlefile", "/pdf/", ".pdf",
    "pdf下载", "下载pdf", "全文下载", "原文下载", "下载全文",
    "/cn/article/", "magtech",
)
_NEGATIVE_MARKERS = (
    "supplement", "supplementary", "figure", "fig.", "table", "cover",
    "logo", "advert", "banner", "share", "weibo", "qrcode",
)


def find_journal_pdf_links(html_text: str, base_url: str) -> list[str]:
    """Return candidate PDF URLs from a Chinese journal landing page.

    Ordered best-first: ``citation_pdf_url`` meta, explicit Magtech
    ``attachType=PDF`` links, then generic PDF links.
    """
    text = html_text or ""
    candidates: list[tuple[int, str]] = []

    for match in re.finditer(
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
        text, re.I,
    ):
        candidates.append((0, _absolute(match.group(1), base_url)))
    for match in re.finditer(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        text, re.I,
    ):
        candidates.append((0, _absolute(match.group(1), base_url)))

    soup = BeautifulSoup(text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        label = f"{href} {anchor.get_text(' ', strip=True)}".lower()
        if any(marker in label for marker in _NEGATIVE_MARKERS):
            continue
        if not any(marker in label for marker in _PDF_MARKERS):
            continue
        score = 10
        if "attachtype=pdf" in label or "downloadarticlefile" in label:
            score = 1
        elif href.lower().endswith(".pdf") or ".pdf?" in href.lower():
            score = 2
        candidates.append((score, _absolute(href, base_url)))

    seen: set[str] = set()
    ordered: list[str] = []
    for _, url in sorted(candidates, key=lambda item: item[0]):
        if url not in seen and url.startswith(("http://", "https://")):
            seen.add(url)
            ordered.append(url)
    return ordered


def _absolute(url: str, base_url: str) -> str:
    url = (url or "").strip().replace("&amp;", "&")
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base_url, url)


def _is_aggregator(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == item or host.endswith("." + item) for item in _AGGREGATOR_HOSTS)


def _session(config: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return session


def _download(session: requests.Session, url: str, referer: str, output_path: Path,
              config: dict[str, Any]) -> bool:
    proxies = proxy_dict(select_proxy_for_url(url, config))
    try:
        resp = session.get(
            url,
            timeout=max(20, int(request_timeout(config)[1])),
            allow_redirects=True,
            stream=True,
            proxies=proxies,
            headers={"Referer": referer, "Accept": "application/pdf,*/*"},
        )
    except requests.RequestException:
        return False
    if resp.status_code >= 400:
        return False
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tmp_path.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    handle.write(chunk)
        if not is_pdf_file(tmp_path):
            return False
        tmp_path.replace(output_path)
        return True
    except OSError:
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def try_cn_journal_direct(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Download a free PDF from a Chinese journal's own website."""
    if not config.get("cn_journal_direct", True):
        return None
    value = normalize_doi_loose(doi)
    if not value:
        return None

    resolution = resolve_chinese_doi(value, config)
    candidates: list[str] = []
    for url in [resolution.landing_url, *resolution.urls]:
        if url and url not in candidates and not _is_aggregator(url):
            candidates.append(url)

    session = _session(config)
    for landing in candidates:
        # Some Chinese journals register a DOI directly at a PDF
        # (e.g. cjc.ict.ac.cn/online/onlinepaper/....pdf).
        if urlparse(landing).path.lower().endswith(".pdf"):
            if _download(session, landing, landing, output_path, config):
                return _journal_success(value, output_path, landing, resolution.title)
            continue

        proxies = proxy_dict(select_proxy_for_url(landing, config))
        try:
            resp = session.get(
                landing,
                timeout=max(20, int(request_timeout(config)[1])),
                allow_redirects=True,
                proxies=proxies,
            )
        except requests.RequestException:
            continue
        if resp.status_code >= 400:
            continue

        links = find_journal_pdf_links(response_text(resp), resp.url or landing)
        for link in links[:6]:
            log.info(f"   [CNJournal] trying PDF link: {link[:100]}")
            if _download(session, link, resp.url or landing, output_path, config):
                return _journal_success(value, output_path, landing, resolution.title)
            time.sleep(0.3)
    return None


def _journal_success(doi: str, output_path: Path, journal_site: str,
                     title: str) -> dict[str, Any]:
    result = success(doi, output_path, "CNJournal")
    result["journal_site"] = journal_site
    if title:
        result["title"] = title
    return result
