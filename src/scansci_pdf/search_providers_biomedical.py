"""PubMed, PMC, bioRxiv, and medRxiv search providers."""

from __future__ import annotations

import os
import threading
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
_NCBI_LOCK = threading.Lock()
_NCBI_LAST_REQUEST = 0.0
_BIORXIV_LOCK = threading.Lock()


def _finish(report: SourceReport, started: float) -> SourceReport:
    report.elapsed_ms = round((time.monotonic() - started) * 1000)
    return report


def _ncbi_params(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    params: dict[str, Any] = {"tool": "scansci_pdf"}
    email = config.get("email", "")
    if valid_contact_email(email):
        params["email"] = email
    api_key = os.environ.get("NCBI_API_KEY") or config.get("ncbi_api_key", "")
    if api_key:
        params["api_key"] = api_key
    return params, bool(api_key)


def _wait_for_ncbi(authenticated: bool) -> None:
    global _NCBI_LAST_REQUEST
    interval = 0.11 if authenticated else 0.34
    delay = interval - (time.monotonic() - _NCBI_LAST_REQUEST)
    if delay > 0:
        time.sleep(delay)


def _mark_ncbi_request() -> None:
    global _NCBI_LAST_REQUEST
    _NCBI_LAST_REQUEST = time.monotonic()


def _extract_article_ids(info: dict[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for article_id in info.get("articleids") or []:
        id_type = str(article_id.get("idtype") or "").lower()
        value = str(article_id.get("value") or "")
        if id_type and value:
            ids[id_type] = value
    return ids


def _parse_ncbi(
    source: str,
    ids: list[str],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    records = payload.get("result") or {}
    for uid in ids:
        info = records.get(uid) or {}
        if not isinstance(info, dict):
            continue
        article_ids = _extract_article_ids(info)
        doi = article_ids.get("doi", "")
        pmid = article_ids.get("pubmed", "") or (
            uid if source == "pubmed" else ""
        )
        pmcid = article_ids.get("pmc", "")
        if source == "pmc" and not pmcid:
            pmcid = "PMC" + uid.removeprefix("PMC")
        pubdate = str(info.get("pubdate") or "")
        title = str(info.get("title") or "").rstrip(".")
        authors = [
            author.get("name", "")
            for author in (info.get("authors") or [])[:10]
            if author.get("name")
        ]
        identifier = doi or pmcid or pmid
        results.append({
            "title": title,
            "doi": doi,
            "identifier": identifier,
            "pmid": pmid,
            "pmcid": pmcid,
            "url": (
                f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                if source == "pubmed"
                else f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
            ),
            "authors": authors,
            "year": pubdate[:4] if pubdate[:4].isdigit() else "",
            "publication_date": pubdate,
            "venue": info.get("fulljournalname") or info.get("source") or "",
            "type": ",".join(info.get("pubtype") or []),
            "cited_by_count": 0,
            "abstract": "",
            "is_oa": source == "pmc",
            "oa_url": (
                f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
                if source == "pmc" else ""
            ),
            "source": source,
        })
    return results


def _search_ncbi(
    source: str,
    spec: SearchSpec,
    config: dict[str, Any],
) -> SourceReport:
    started = time.monotonic()
    report = SourceReport(source=source)
    db = "pubmed" if source == "pubmed" else "pmc"
    common, authenticated = _ncbi_params(config)
    report.authenticated = authenticated
    query, warnings = adapt_query(spec.effective_query, source, exact=spec.exact)
    report.warnings.extend(warnings)

    identifier_type, identifier = spec.identifier
    if identifier_type == "doi":
        query = f'"{identifier}"[AID]'
    for publication_type in spec.publication_types:
        query = f"({query}) AND ({publication_type}[PT])" if query else f"{publication_type}[PT]"
    if spec.language:
        query = f"({query}) AND ({spec.language}[LA])" if query else f"{spec.language}[LA]"
    if spec.has_abstract is True:
        query = f"({query}) AND hasabstract" if query else "hasabstract"
    elif spec.has_abstract is False:
        query = f"({query}) NOT hasabstract" if query else "NOT hasabstract"
    if spec.fields_of_study:
        additions = " AND ".join(f'"{value}"' for value in spec.fields_of_study)
        query = f"({query}) AND ({additions})" if query else additions
        report.warnings.append(
            f"{source} fields_of_study values were searched as text; use mesh: for exact MeSH headings"
        )
    if spec.open_access_only and source == "pubmed":
        query = f"({query}) AND pmc[filter]" if query else "pmc[filter]"
    if spec.min_citations is not None:
        report.warnings.append(f"{source} does not expose citation-count filtering")
    if spec.category:
        report.warnings.append(
            f"{source} does not expose the arXiv/preprint category filter"
        )

    params: dict[str, Any] = {
        "db": db,
        "term": query,
        "retmax": spec.limit,
        "retstart": spec.offset,
        "retmode": "json",
        **common,
    }
    if spec.date_from or spec.date_to:
        params["datetype"] = "pdat"
        if spec.date_from:
            params["mindate"] = spec.date_from.replace("-", "/")
        if spec.date_to:
            params["maxdate"] = spec.date_to.replace("-", "/")
    if spec.recent_days:
        params["reldate"] = spec.recent_days
        params["datetype"] = "pdat"
    if spec.sort in {"publication_date", "updated_date"}:
        params["sort"] = "pub_date"
    else:
        params["sort"] = "relevance"
    if spec.sort == "cited_by_count":
        report.warnings.append(
            f"{source} cannot sort by citations; merged results are sorted locally"
        )

    report.endpoint = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    )
    report.params = params
    try:
        with _NCBI_LOCK, requests.Session() as session:
            session.headers.update({
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            })
            _wait_for_ncbi(authenticated)
            response = session.get(report.endpoint, params=params, timeout=_TIMEOUT)
            _mark_ncbi_request()
            response.raise_for_status()
            search_payload = response.json().get("esearchresult") or {}
            ids = search_payload.get("idlist") or []
            report.total = int(search_payload.get("count") or 0)
            if not ids:
                return _finish(report, started)
            summary_params = {
                "db": db,
                "id": ",".join(ids),
                "retmode": "json",
                **common,
            }
            _wait_for_ncbi(authenticated)
            summary_response = session.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params=summary_params,
                timeout=_TIMEOUT,
            )
            _mark_ncbi_request()
            summary_response.raise_for_status()
            report.results = _parse_ncbi(source, ids, summary_response.json())
            for item in report.results:
                if spec.has_abstract is not None:
                    item["has_abstract"] = spec.has_abstract
                if spec.open_access_only and source == "pubmed":
                    item["is_oa"] = True
                    if item.get("pmcid"):
                        item["oa_url"] = (
                            "https://pmc.ncbi.nlm.nih.gov/articles/"
                            + item["pmcid"] + "/"
                        )
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        report.error = f"invalid NCBI response: {exc}"
    return _finish(report, started)


def search_pubmed(spec: SearchSpec, config: dict[str, Any]) -> SourceReport:
    return _search_ncbi("pubmed", spec, config)


def search_pmc(spec: SearchSpec, config: dict[str, Any]) -> SourceReport:
    return _search_ncbi("pmc", spec, config)


def _parse_preprints(
    source: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        doi = item.get("doi") or ""
        published_doi = item.get("published") or ""
        if str(published_doi).upper() == "NA":
            published_doi = ""
        author_text = item.get("authors") or ""
        authors = [
            name.strip() for name in str(author_text).split(";") if name.strip()
        ]
        publication_date = item.get("date") or ""
        results.append({
            "title": item.get("title") or "",
            "doi": doi,
            "identifier": doi,
            "published_doi": published_doi,
            "url": f"https://doi.org/{doi}" if doi else "",
            "authors": authors[:10],
            "year": str(publication_date)[:4],
            "publication_date": publication_date,
            "venue": source,
            "type": item.get("type") or "preprint",
            "category": item.get("category") or "",
            "license": item.get("license") or "",
            "version": item.get("version") or "",
            "cited_by_count": 0,
            "abstract": (item.get("abstract") or "")[:2000],
            "is_oa": True,
            "oa_url": f"https://doi.org/{doi}" if doi else "",
            "source": source,
        })
    return results


def _search_preprint_server(
    source: str,
    spec: SearchSpec,
    _config: dict[str, Any],
) -> SourceReport:
    started = time.monotonic()
    report = SourceReport(source=source)
    identifier_type, identifier = spec.identifier
    if spec.query and identifier_type != "doi":
        report.warnings.append(
            f"{source} has no keyword API; use OpenAlex or Semantic Scholar for topic search"
        )
        report.total = 0
        return _finish(report, started)

    if identifier_type == "doi":
        interval = quote(identifier, safe="/")
        cursor = "na"
    elif spec.recent_days:
        interval = f"{spec.recent_days}d"
        cursor = str(spec.offset)
    elif spec.date_from or spec.date_to:
        start_date = spec.date_from or spec.date_to
        end_date = spec.date_to or spec.date_from
        interval = f"{start_date}/{end_date}"
        cursor = str(spec.offset)
    else:
        report.warnings.append(
            f"{source} browse requires date_from/date_to, recent_days, or an exact DOI"
        )
        report.total = 0
        return _finish(report, started)

    if spec.offset and identifier_type == "doi":
        report.warnings.append(f"{source} DOI lookup ignores offset")
    if spec.limit != 100 and identifier_type != "doi":
        report.warnings.append(
            f"{source} API pages contain 100 records; results are locally limited to {spec.limit}"
        )
    if spec.publication_types or spec.fields_of_study or spec.min_citations is not None:
        report.warnings.append(
            f"{source} does not support publication type, field, or citation filters"
        )
    if spec.language or spec.venue:
        report.warnings.append(
            f"{source} does not support language or venue filters"
        )

    report.endpoint = (
        f"https://api.biorxiv.org/details/{source}/{interval}/{cursor}/json"
    )
    params: dict[str, Any] = {}
    if spec.category:
        params["category"] = spec.category
    report.params = params
    try:
        with _BIORXIV_LOCK, requests.Session() as session:
            session.headers.update({
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            })
            response = session.get(report.endpoint, params=params, timeout=_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            messages = payload.get("messages") or []
            if messages:
                report.total = int(messages[0].get("total") or messages[0].get("count") or 0)
            results = _parse_preprints(source, payload.get("collection") or [])
            if spec.has_abstract is not None:
                results = [
                    item for item in results
                    if bool(item.get("abstract")) is spec.has_abstract
                ]
            report.results = results[:spec.limit]
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        report.error = f"invalid {source} response: {exc}"
    return _finish(report, started)


def search_biorxiv(spec: SearchSpec, config: dict[str, Any]) -> SourceReport:
    return _search_preprint_server("biorxiv", spec, config)


def search_medrxiv(spec: SearchSpec, config: dict[str, Any]) -> SourceReport:
    return _search_preprint_server("medrxiv", spec, config)
