"""Authorized SAGE China downloads backed by a saved CARSI session."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from ..config import DATA_DIR
from ..log import get_logger
from ..network import USER_AGENT
from ..pdf_utils import _response_looks_pdf, is_pdf_file
from .publishers import _load_publisher_cookies, _write_pdf_atomic

log = get_logger()

SAGE_CN_ARTICLE_URL = "https://sage.cnpereading.com/doi/{doi}"
SAGE_CN_DOWNLOAD_URL = "https://sage.cnpereading.com/website/journal/download"
_AUTH_COOKIE_NAMES = {"jsessionid", "token", "userinfo"}

_ARTICLE_RECORD_RE = re.compile(
    r'articleId\s*":\s*"(?P<article_id>[A-F0-9]{32})"\s*,\s*"doi\s*":\s*"(?P<doi>[^"<]+)',
    re.IGNORECASE,
)


def extract_article_id(html: str, doi: str) -> str:
    """Return the SAGE CN article id paired with *doi*, ignoring related papers."""
    normalized_html = html.replace(r'\"', '"').replace(r"\/", "/")
    wanted = doi.strip().lower()
    for match in _ARTICLE_RECORD_RE.finditer(normalized_html):
        candidate = match.group("doi").replace("\\", "").strip().lower()
        if candidate == wanted:
            return match.group("article_id")
    return ""


def _cookie_files(config: dict[str, Any]) -> list[Path]:
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    return [
        cache_dir / "carsi_cookies" / "sage-cn.json",
        cache_dir / "carsi_cookies" / "sage.json",
        cache_dir / "publisher_cookies.json",
    ]


def has_saved_sage_cn_session(config: dict[str, Any]) -> bool:
    """Whether an unexpired saved cookie targets the SAGE China domain."""
    now = time.time()
    for cookie_file in _cookie_files(config):
        if not cookie_file.exists():
            continue
        try:
            cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(cookies, list):
            continue
        for cookie in cookies:
            domain = str(cookie.get("domain", "")).lstrip(".").lower()
            try:
                expires = float(cookie.get("expires", 0) or 0)
            except (TypeError, ValueError):
                expires = 0
            if (
                str(cookie.get("name", "")).lower() in _AUTH_COOKIE_NAMES
                and cookie.get("value")
                and (
                    domain == "sage.cnpereading.com"
                    or domain.endswith(".sage.cnpereading.com")
                )
                and (not expires or expires > now)
            ):
                return True
    return False


def try_sage_cn_authorized(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    *,
    session: requests.Session | None = None,
) -> bool:
    """Download a SAGE CN PDF through a previously authorized CARSI session.

    SAGE CN does not expose the article PDF at ``/doi/pdf/<doi>``. Its page
    data pairs each DOI with an opaque ``articleId``; the visible PDF button
    then calls ``/website/journal/download?articleId=...``. Replaying that
    site-owned request with the saved institutional cookies avoids browser PDF
    viewer crashes while preserving the user's authorized access.
    """
    if not has_saved_sage_cn_session(config):
        return False

    own_session = session is None
    client = session or requests.Session()
    if own_session:
        client.trust_env = False
    client.headers.setdefault("User-Agent", USER_AGENT)
    client.headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    _load_publisher_cookies(client, config)

    timeout = int(config.get("publisher_timeout", 60) or 60)
    article_url = SAGE_CN_ARTICLE_URL.format(doi=doi)
    try:
        article_response = client.get(article_url, timeout=timeout, allow_redirects=True)
        if article_response.status_code >= 400:
            return False
        article_id = extract_article_id(article_response.text, doi)
        if not article_id:
            return False

        pdf_response = client.get(
            SAGE_CN_DOWNLOAD_URL,
            params={"articleId": article_id},
            timeout=timeout,
            stream=True,
            allow_redirects=True,
            headers={"Accept": "application/pdf,*/*", "Referer": article_url},
        )
        if pdf_response.status_code >= 400:
            return False
        iterator = pdf_response.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not _response_looks_pdf(pdf_response, first):
            return False
        if not _write_pdf_atomic(output_path, first, iterator):
            return False
        if not is_pdf_file(output_path):
            return False
        log.info("   [SAGE-CN] authorized PDF downloaded via articleId")
        return True
    except (requests.RequestException, OSError, ValueError):
        return False
