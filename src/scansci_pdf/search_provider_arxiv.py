"""arXiv Atom search provider."""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from .search_query import SearchSpec, SourceReport, adapt_query

_TIMEOUT = 30
_USER_AGENT = "scansci-pdf/1.9 (+https://github.com/Rimagination/scansci-pdf)"
_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST_REQUEST = 0.0

_ATOM = "http://www.w3.org/2005/Atom"
_ARXIV = "http://arxiv.org/schemas/atom"
_OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"


def _finish(report: SourceReport, started: float) -> SourceReport:
    report.elapsed_ms = round((time.monotonic() - started) * 1000)
    return report


def _parse_feed(root: ET.Element) -> tuple[list[dict[str, Any]], int | None]:
    total_text = root.findtext(f"{{{_OPENSEARCH}}}totalResults")
    total = int(total_text) if total_text and total_text.isdigit() else None
    results: list[dict[str, Any]] = []
    for entry in root.findall(f"{{{_ATOM}}}entry"):
        raw_id = entry.findtext(f"{{{_ATOM}}}id") or ""
        arxiv_id = raw_id.rstrip("/").rsplit("/", 1)[-1]
        title = " ".join((entry.findtext(f"{{{_ATOM}}}title") or "").split())
        abstract = " ".join(
            (entry.findtext(f"{{{_ATOM}}}summary") or "").split()
        )[:2000]
        published = entry.findtext(f"{{{_ATOM}}}published") or ""
        updated = entry.findtext(f"{{{_ATOM}}}updated") or ""
        doi = entry.findtext(f"{{{_ARXIV}}}doi") or ""
        journal = entry.findtext(f"{{{_ARXIV}}}journal_ref") or ""
        authors = [
            author.findtext(f"{{{_ATOM}}}name") or ""
            for author in entry.findall(f"{{{_ATOM}}}author")
        ]
        categories = [
            category.get("term", "")
            for category in entry.findall(f"{{{_ATOM}}}category")
            if category.get("term")
        ]
        pdf_url = ""
        for link in entry.findall(f"{{{_ATOM}}}link"):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
                break
        results.append({
            "title": title,
            "doi": doi,
            "identifier": doi or arxiv_id,
            "arxiv_id": arxiv_id,
            "url": raw_id,
            "authors": [author for author in authors if author][:10],
            "year": published[:4],
            "publication_date": published,
            "updated_date": updated,
            "venue": journal or "arXiv",
            "type": "preprint",
            "category": ",".join(categories),
            "cited_by_count": 0,
            "abstract": abstract,
            "is_oa": True,
            "oa_url": pdf_url,
            "source": "arxiv",
        })
    return results, total


def search_arxiv(spec: SearchSpec, _config: dict[str, Any]) -> SourceReport:
    global _ARXIV_LAST_REQUEST
    started = time.monotonic()
    report = SourceReport(
        source="arxiv",
        endpoint="https://export.arxiv.org/api/query",
    )
    identifier_type, identifier = spec.identifier
    params: dict[str, Any] = {
        "start": spec.offset,
        "max_results": min(spec.limit, 100),
        "sortOrder": "descending",
    }
    if identifier_type == "arxiv":
        params["id_list"] = identifier
    else:
        query, warnings = adapt_query(
            spec.effective_query, "arxiv", exact=spec.exact
        )
        report.warnings.extend(warnings)
        clauses = [query] if query else []
        if spec.category:
            clauses.append(f"cat:{spec.category}")
        if spec.date_from or spec.date_to:
            lower = (spec.date_from or "1900-01-01").replace("-", "") + "0000"
            upper = (spec.date_to or "2999-12-31").replace("-", "") + "2359"
            clauses.append(f"submittedDate:[{lower} TO {upper}]")
        if spec.fields_of_study:
            clauses.extend(f'all:"{value}"' for value in spec.fields_of_study)
            report.warnings.append(
                "arXiv fields_of_study values were searched as text; use category for exact arXiv taxonomy"
            )
        if spec.publication_types:
            report.warnings.append("arXiv only contains preprints; publication_types was ignored")
        if spec.language:
            report.warnings.append("arXiv has no language filter")
        if spec.min_citations is not None:
            report.warnings.append("arXiv has no citation-count filter")
        if spec.has_abstract is False:
            report.warnings.append("arXiv records include abstracts; has_abstract=false was ignored")
        if spec.recent_days:
            report.warnings.append(
                "arXiv does not support recent_days; use date_from/date_to"
            )
        if not clauses:
            report.warnings.append("arXiv requires a query or arXiv ID")
            report.total = 0
            return _finish(report, started)
        params["search_query"] = " AND ".join(clauses)

    params["sortBy"] = {
        "relevance": "relevance",
        "publication_date": "submittedDate",
        "updated_date": "lastUpdatedDate",
        "cited_by_count": "relevance",
    }[spec.sort]
    if spec.sort == "cited_by_count":
        report.warnings.append(
            "arXiv has no citation sort; relevance was used and merged results are sorted locally"
        )
    report.params = params

    try:
        with _ARXIV_LOCK:
            delay = 3.0 - (time.monotonic() - _ARXIV_LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
            with requests.Session() as session:
                session.headers.update({
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/atom+xml",
                })
                response = session.get(
                    report.endpoint,
                    params=params,
                    timeout=_TIMEOUT,
                )
                _ARXIV_LAST_REQUEST = time.monotonic()
                response.raise_for_status()
                report.results, report.total = _parse_feed(
                    ET.fromstring(response.content)
                )
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (ET.ParseError, TypeError, ValueError) as exc:
        report.error = f"invalid arXiv response: {exc}"
    return _finish(report, started)
