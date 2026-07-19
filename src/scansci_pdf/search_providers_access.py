"""CORE and Unpaywall search/access providers."""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import requests

from .search_query import (
    SearchSpec,
    SourceReport,
    adapt_query,
    valid_contact_email,
)

_TIMEOUT = 30
_USER_AGENT = "scansci-pdf/1.9 (+https://github.com/Rimagination/scansci-pdf)"


def _finish(report: SourceReport, started: float) -> SourceReport:
    report.elapsed_ms = round((time.monotonic() - started) * 1000)
    return report


def _parse_core(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        doi = item.get("doi") or ""
        authors = item.get("authors") or []
        names = [
            author.get("name", "") if isinstance(author, dict) else str(author)
            for author in authors[:10]
        ]
        language = item.get("language") or {}
        providers = item.get("dataProviders") or []
        venue = ""
        if providers:
            first = providers[0]
            venue = first.get("name", "") if isinstance(first, dict) else str(first)
        download_url = item.get("downloadUrl") or ""
        if not download_url:
            download_url = next(
                (
                    link.get("url", "")
                    for link in (item.get("links") or [])
                    if isinstance(link, dict) and link.get("type") == "download"
                ),
                "",
            )
        results.append({
            "title": item.get("title") or "",
            "doi": doi,
            "identifier": doi or str(item.get("id") or ""),
            "core_id": str(item.get("id") or ""),
            "arxiv_id": item.get("arxivId") or "",
            "url": download_url,
            "authors": [name for name in names if name],
            "year": item.get("yearPublished") or "",
            "publication_date": item.get("publishedDate") or "",
            "venue": venue,
            "type": item.get("documentType") or "",
            "language": language.get("code", "") if isinstance(language, dict) else language,
            "cited_by_count": item.get("citationCount") or 0,
            "abstract": (item.get("abstract") or "")[:2000],
            "is_oa": True,
            "oa_url": download_url,
            "source": "core",
        })
    return results


def search_core(spec: SearchSpec, config: dict[str, Any]) -> SourceReport:
    started = time.monotonic()
    report = SourceReport(source="core")
    api_key = os.environ.get("CORE_API_KEY") or config.get("core_api_key", "")
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        report.authenticated = True

    query, warnings = adapt_query(spec.effective_query, "core", exact=spec.exact)
    report.warnings.extend(warnings)
    clauses = [query] if query else []
    if spec.date_from:
        clauses.append(f'yearPublished>={spec.date_from[:4]}')
    if spec.date_to:
        clauses.append(f'yearPublished<={spec.date_to[:4]}')
    if spec.publication_types:
        values = " OR ".join(
            f'documentType:"{value}"' for value in spec.publication_types
        )
        clauses.append(f"({values})")
    if spec.language:
        clauses.append(f'language.code:"{spec.language.lower()}"')
    if spec.has_abstract is True:
        clauses.append("_exists_:abstract")
    if spec.has_abstract is False:
        clauses.append("NOT _exists_:abstract")
    if spec.fields_of_study:
        clauses.extend(f'"{value}"' for value in spec.fields_of_study)
        report.warnings.append(
            "CORE fields_of_study values were added to full-text search"
        )
    if spec.category:
        report.warnings.append(
            "CORE does not expose the arXiv/preprint category filter"
        )
    if spec.recent_days:
        report.warnings.append(
            "CORE does not support recent_days; use date_from/date_to"
        )
    params: dict[str, Any] = {
        "q": " AND ".join(filter(None, clauses)),
        "limit": spec.limit,
        "offset": spec.offset,
        "sort": "recency" if spec.sort in {"publication_date", "updated_date"} else "relevance",
    }
    if spec.sort == "cited_by_count":
        report.warnings.append(
            "CORE cannot sort by citation count; merged results are sorted locally"
        )
    report.endpoint = "https://api.core.ac.uk/v3/search/works/"
    report.params = params
    try:
        with requests.Session() as session:
            session.headers.update(headers)
            response = session.get(report.endpoint, params=params, timeout=_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            report.results = _parse_core(payload.get("results", []))
            report.total = payload.get("totalHits")
            if payload.get("errors"):
                report.warnings.append("CORE reported partial shard errors")
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        report.error = f"invalid CORE response: {exc}"
    return _finish(report, started)


def _parse_unpaywall(item: dict[str, Any]) -> dict[str, Any]:
    doi = item.get("doi") or ""
    best = item.get("best_oa_location") or {}
    return {
        "title": item.get("title") or "",
        "doi": doi,
        "identifier": doi,
        "url": item.get("doi_url") or (f"https://doi.org/{doi}" if doi else ""),
        "authors": [
            author.get("raw_author_name", "")
            for author in (item.get("z_authors") or [])[:10]
            if author.get("raw_author_name")
        ],
        "year": item.get("year") or "",
        "publication_date": item.get("published_date") or "",
        "venue": item.get("journal_name") or "",
        "type": item.get("genre") or "",
        "cited_by_count": 0,
        "abstract": "",
        "is_oa": bool(item.get("is_oa")),
        "oa_status": item.get("oa_status") or "",
        "oa_url": best.get("url_for_pdf") or best.get("url")
                  or best.get("url_for_landing_page") or "",
        "oa_host_type": best.get("host_type") or "",
        "oa_version": best.get("version") or "",
        "license": best.get("license") or "",
        "source": "unpaywall",
    }


def search_unpaywall(spec: SearchSpec, config: dict[str, Any]) -> SourceReport:
    started = time.monotonic()
    report = SourceReport(source="unpaywall")
    identifier_type, identifier = spec.identifier
    email = config.get("email", "")
    report.endpoint = "https://api.unpaywall.org/v2/{doi}"
    if identifier_type != "doi":
        report.warnings.append(
            "Unpaywall is DOI lookup only; discover papers in another source first"
        )
        report.total = 0
        return _finish(report, started)
    if not valid_contact_email(email):
        report.warnings.append(
            "Unpaywall requires a real contact email; set config key 'email'"
        )
        report.total = 0
        return _finish(report, started)

    endpoint = "https://api.unpaywall.org/v2/" + quote(identifier, safe="/")
    report.endpoint = endpoint
    report.params = {"email": email}
    try:
        with requests.Session() as session:
            session.headers.update({
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            })
            response = session.get(endpoint, params=report.params, timeout=_TIMEOUT)
            if response.status_code == 404:
                report.total = 0
                return _finish(report, started)
            response.raise_for_status()
            report.results = [_parse_unpaywall(response.json())]
            report.total = 1
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        report.error = f"invalid Unpaywall response: {exc}"
    return _finish(report, started)


def enrich_unpaywall(
    dois: list[str],
    config: dict[str, Any],
    *,
    max_items: int = 10,
) -> SourceReport:
    """Bounded sequential DOI enrichment; never fan out without an explicit request."""
    started = time.monotonic()
    report = SourceReport(
        source="unpaywall",
        endpoint="https://api.unpaywall.org/v2/{doi}",
    )
    email = config.get("email", "")
    report.params = {
        "email": email,
        "doi_count": min(len(dois), max_items),
    }
    if not valid_contact_email(email):
        report.warnings.append(
            "Unpaywall enrichment skipped: set a real contact email in config key 'email'"
        )
        report.total = 0
        return _finish(report, started)
    unique_dois = list(dict.fromkeys(doi for doi in dois if doi))[:max_items]
    try:
        with requests.Session() as session:
            session.headers.update({
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            })
            for doi in unique_dois:
                endpoint = "https://api.unpaywall.org/v2/" + quote(doi, safe="/")
                response = session.get(
                    endpoint,
                    params={"email": email},
                    timeout=_TIMEOUT,
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                report.results.append(_parse_unpaywall(response.json()))
        report.total = len(report.results)
        if len(dois) > max_items:
            report.warnings.append(
                f"Unpaywall enrichment is bounded to the first {max_items} unique DOIs"
            )
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        report.error = f"invalid Unpaywall response: {exc}"
    return _finish(report, started)
