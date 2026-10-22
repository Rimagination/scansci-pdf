"""NCPSSD (国家哲学社会科学文献中心) full-text source.

NCPSSD is a legal, free full-text archive for Chinese humanities and social
science journals.  Its public search endpoint returns article metadata and,
for many records, a direct PDF URL.  It is the best first stop for Chinese
DOIs: no login, no anti-bot browser, and the PDF is hosted by the platform.

The endpoint is a plain form POST; the ``search`` field is the *raw* Solr-ish
expression (not the Base64 form seen in the page URL).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import requests

from ..chinese import (
    journal_metadata_from_url,
    normalize_cn_text,
    normalize_doi_loose,
    resolve_chinese_doi,
    title_similarity,
)
from ..log import get_logger
from ..network import CHROME_UA, proxy_dict, request_timeout, select_proxy_for_url
from ..pdf_utils import is_pdf_file, success

log = get_logger()

NCPSSD_BASE = "https://www.ncpssd.cn"
NCPSSD_SEARCH_URL = f"{NCPSSD_BASE}/searchHandler/search"
NCPSSD_ARTICLE_URL = f"{NCPSSD_BASE}/Literature/articleinfo"

_SEARCH_SORT = "synUpdateType|DESC,date|DESC,ik_subject|DESC,id|DESC"


def _session(config: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": NCPSSD_BASE,
        "Referer": f"{NCPSSD_BASE}/Literature/articlelist",
    })
    return session


def _post(
    session: requests.Session,
    url: str,
    data: dict[str, str],
    config: dict[str, Any],
    *,
    retries: int = 3,
) -> requests.Response | None:
    cfg = config or {}
    timeout = max(15, int(request_timeout(cfg)[1]))
    proxies = proxy_dict(select_proxy_for_url(url, cfg))
    for attempt in range(retries):
        try:
            resp = session.post(url, data=data, timeout=timeout, proxies=proxies)
            if resp.status_code < 500:
                return resp
        except requests.RequestException as exc:
            log.debug("NCPSSD request failed (%s/%s): %s", attempt + 1, retries, exc)
        if attempt + 1 < retries:
            time.sleep(0.6 * (attempt + 1))
    return None


def _escape_expression(value: str) -> str:
    value = (value or "").replace("\\", " ").replace('"', " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def search_ncpssd(
    query: str,
    config: dict[str, Any] | None = None,
    *,
    page_size: int = 10,
) -> list[dict[str, Any]]:
    """Search NCPSSD by title.  Returns raw rows (dicts)."""
    cfg = config or {}
    query = _escape_expression(query)
    if not query:
        return []
    session = _session(cfg)
    expression = f'(IKTE="{query}")'
    data = {
        "search": expression,
        "pageNum": "1",
        "pageSize": str(max(1, min(page_size, 50))),
        "sort": _SEARCH_SORT,
        "sType": "0",
        "ajaxKeys": "",
        "customShowCondition": "",
    }
    resp = _post(session, NCPSSD_SEARCH_URL, data, cfg)
    if not resp or resp.status_code >= 400:
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []
    rows = ((payload.get("data") or {}).get("rows") or [])
    return [row for row in rows if isinstance(row, dict)]


def search_ncpssd_by_title(
    title: str,
    config: dict[str, Any] | None = None,
    *,
    threshold: float = 0.86,
) -> dict[str, Any] | None:
    """Find the NCPSSD record that best matches *title*.

    Exact punctuation differences are normalised away.  When the full title
    returns nothing, a shorter prefix query is tried once (NCPSSD's phrase
    search is strict about very long subtitles).
    """
    title = normalize_cn_text(title)
    if not title or len(title) < 4:
        return None
    queries = [title]
    # NCPSSD phrase search is punctuation-sensitive in places: retry with
    # full-width brackets and with the part before a parenthetical subtitle.
    fullwidth = title.replace("(", "（").replace(")", "）")
    if fullwidth != title:
        queries.append(fullwidth)
    if "(" in title:
        queries.append(title.split("(", 1)[0])
    if len(title) > 14:
        queries.append(title[:14])
    best: dict[str, Any] | None = None
    best_score = 0.0
    for query in queries:
        rows = search_ncpssd(query, config, page_size=10)
        for row in rows:
            candidate = normalize_cn_text(str(row.get("title") or ""))
            score = title_similarity(title, candidate)
            if row.get("pdfurl"):
                score += 0.02  # prefer records that can actually be downloaded
            if score > best_score:
                best, best_score = row, score
        if best_score >= 0.92:
            break
    if best is not None and best_score >= threshold:
        return best
    return None


def _atomic_write_pdf(resp: requests.Response, output_path: Path) -> bool:
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
    except OSError as exc:
        log.debug("NCPSSD PDF write failed: %s", exc)
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def download_ncpssd_row(
    row: dict[str, Any],
    doi: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Download the PDF URL carried by an NCPSSD search row."""
    pdf_url = str(row.get("pdfurl") or "").strip()
    if not pdf_url.startswith(("http://", "https://")):
        return None
    session = _session(config)
    cfg = config or {}
    timeout = max(30, int(request_timeout(cfg)[1]))
    proxies = proxy_dict(select_proxy_for_url(pdf_url, cfg))
    try:
        resp = session.get(
            pdf_url,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
            proxies=proxies,
            headers={"Referer": f"{NCPSSD_BASE}/", "Accept": "application/pdf,*/*"},
        )
    except requests.RequestException as exc:
        log.debug("NCPSSD PDF request failed: %s", exc)
        return None
    if resp.status_code >= 400:
        return None
    if not _atomic_write_pdf(resp, output_path):
        return None
    result = success(doi, output_path, "NCPSSD")
    result["ncpssd_id"] = row.get("id") or row.get("data_id") or ""
    result["title"] = normalize_cn_text(str(row.get("title") or ""))
    result["issn"] = row.get("issn") or ""
    result["journal"] = row.get("cbw_name") or row.get("periodName") or ""
    result["year"] = row.get("years") or ""
    return result


def try_ncpssd(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Try NCPSSD for a Chinese DOI's free full text."""
    if not config.get("ncpssd_enabled", True):
        return None
    value = normalize_doi_loose(doi)
    if not value:
        return None

    resolution = resolve_chinese_doi(value, config)
    title = resolution.title
    if not title:
        # ``title_from_html`` may have failed for a SPA landing page; NCPSSD
        # itself is title-search only, so there is nothing safe to query.
        return None

    row = search_ncpssd_by_title(title, config)
    if not row:
        return None
    # If the DOI is present in the record, require it to agree.  NCPSSD often
    # omits the DOI, so absence is acceptable; a different DOI is not.
    row_doi = normalize_doi_loose(str(row.get("doi") or ""))
    if row_doi and value and row_doi.lower() != value.lower():
        log.info(f"   [NCPSSD] title match but DOI differs ({row_doi} != {value})")
        return None
    if row.get("pdfurl"):
        log.info(f"   [NCPSSD] free full text: {normalize_cn_text(str(row.get('title') or ''))[:50]}")
        return download_ncpssd_row(row, value, output_path, config)
    log.info("   [NCPSSD] indexed but no free PDF for this record")
    return None


def row_to_crossref(row: dict[str, Any], doi: str) -> dict[str, Any]:
    """Convert an NCPSSD row into Crossref-like metadata for rename/BibTeX."""
    year = str(row.get("years") or "").strip()
    authors = _parse_creators(str(row.get("creator") or row.get("writerAll") or ""))
    page = ""
    if row.get("beginpage"):
        page = str(row["beginpage"])
        if row.get("endpage"):
            page += f"-{row['endpage']}"
    volume = str(row.get("volumn") or "").strip()
    issue = str(row.get("num") or row.get("ssnum") or "").strip()
    journal = normalize_cn_text(str(row.get("cbw_name") or row.get("periodName") or ""))
    message: dict[str, Any] = {
        "title": [normalize_cn_text(str(row.get("title") or ""))],
        "author": [{"family": name} for name in authors],
        "container-title": [journal] if journal else [],
        "DOI": doi,
        "URL": f"https://doi.org/{doi}" if doi else "",
        "type": "journal-article",
        "publisher": normalize_cn_text(str(row.get("tspress") or "")) or "国家哲学社会科学文献中心",
    }
    if year.isdigit():
        message["published-print"] = {"date-parts": [[int(year)]]}
    if volume and volume.isdigit():
        message["volume"] = volume
    if issue and re.fullmatch(r"\d+", issue):
        message["issue"] = issue
    if page:
        message["page"] = page
    if row.get("issn"):
        message["ISSN"] = [str(row["issn"])]
    abstract = normalize_cn_text(str(row.get("remark") or row.get("ik_remark") or ""))
    if abstract:
        message["abstract"] = abstract
    return message


def _parse_creators(value: str) -> list[str]:
    names: list[str] = []
    for part in re.split(r"[;；]", value or ""):
        name = re.sub(r"\[[\d,\s]+\]", "", part).strip()
        name = normalize_cn_text(name)
        if name:
            names.append(name)
    return names


def chinese_metadata(doi: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """Metadata fallback for Chinese DOIs that Crossref does not index.

    Resolution gives us the title (chndoi handler / CNKI / Wanfang landing);
    NCPSSD then supplies authors, journal, year, pages, and ISSN.
    """
    value = normalize_doi_loose(doi)
    if not value:
        return None
    resolution = resolve_chinese_doi(value, config)
    title = resolution.title
    if not title:
        return None
    row = search_ncpssd_by_title(title, config)
    if row:
        row_doi = normalize_doi_loose(str(row.get("doi") or ""))
        if not row_doi or row_doi.lower() == value.lower():
            return row_to_crossref(row, value)

    # NCPSSD may not index the journal.  Chinese journals that host their own
    # site usually expose full citation meta, so use it for year/journal/pages.
    for url in resolution.urls:
        meta = journal_metadata_from_url(url, config)
        if meta:
            meta.setdefault("DOI", value)
            return meta

    return resolution.as_metadata() or None


def ncpssd_record_url(article_id: str) -> str:
    """Build a human-openable NCPSSD article URL."""
    return f"{NCPSSD_ARTICLE_URL}?id={article_id}"
