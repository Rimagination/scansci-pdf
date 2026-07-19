"""OpenAlex, Semantic Scholar, and Crossref search providers."""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import quote

import requests

from .search_query import (
    SearchSpec,
    SourceReport,
    adapt_query,
    extract_fields,
    quote_value,
    valid_contact_email,
)

_TIMEOUT = 30
_USER_AGENT = "scansci-pdf/1.9 (+https://github.com/Rimagination/scansci-pdf)"


def _session(headers: dict[str, str] | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT, "Accept": "application/json"})
    if headers:
        session.headers.update(headers)
    return session


def _finish(report: SourceReport, started: float) -> SourceReport:
    report.elapsed_ms = round((time.monotonic() - started) * 1000)
    return report


def _year_from_date(value: Any) -> Any:
    if isinstance(value, dict):
        parts = value.get("date-parts") or [[]]
        return parts[0][0] if parts and parts[0] else ""
    return ""


def _parse_openalex(works: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for work in works:
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        ids = work.get("ids") or {}
        oa = work.get("open_access") or {}
        best = work.get("best_oa_location") or {}
        primary = work.get("primary_location") or {}
        source = primary.get("source") or {}
        authors = [
            entry.get("author", {}).get("display_name", "")
            for entry in (work.get("authorships") or [])[:10]
            if entry.get("author", {}).get("display_name")
        ]
        abstract_index = work.get("abstract_inverted_index")
        abstract = ""
        if isinstance(abstract_index, dict):
            positions: list[tuple[int, str]] = []
            for word, indexes in abstract_index.items():
                if isinstance(indexes, list):
                    positions.extend((index, word) for index in indexes if isinstance(index, int))
            abstract = " ".join(word for _, word in sorted(positions))[:2000]
        pmid = str(ids.get("pmid") or "").rsplit("/", 1)[-1]
        results.append({
            "title": work.get("title") or work.get("display_name") or "",
            "doi": doi,
            "identifier": doi or work.get("id", "").rsplit("/", 1)[-1],
            "openalex_id": work.get("id", "").rsplit("/", 1)[-1],
            "pmid": pmid if pmid.isdigit() else "",
            "url": work.get("id") or "",
            "authors": authors,
            "year": work.get("publication_year") or "",
            "publication_date": work.get("publication_date") or "",
            "venue": source.get("display_name") or "",
            "type": work.get("type") or "",
            "cited_by_count": work.get("cited_by_count") or 0,
            "abstract": abstract,
            "is_oa": bool(oa.get("is_oa")),
            "oa_status": oa.get("oa_status") or "",
            "oa_url": best.get("pdf_url") or best.get("landing_page_url")
                      or oa.get("oa_url") or "",
            "source": "openalex",
        })
    return results


def search_openalex(spec: SearchSpec, config: dict[str, Any]) -> SourceReport:
    started = time.monotonic()
    report = SourceReport(source="openalex")
    api_key = os.environ.get("OPENALEX_API_KEY") or config.get("openalex_api_key", "")
    contact = config.get("email", "")
    params: dict[str, Any] = {}
    if api_key:
        params["api_key"] = api_key
        report.authenticated = True
    elif valid_contact_email(contact):
        params["mailto"] = contact

    identifier_type, identifier = spec.identifier
    try:
        with _session() as session:
            if identifier_type == "doi":
                report.endpoint = (
                    "https://api.openalex.org/works/doi:" + quote(identifier, safe="")
                )
                report.params = params
                response = session.get(report.endpoint, params=params, timeout=_TIMEOUT)
                if response.status_code == 404:
                    report.total = 0
                    return _finish(report, started)
                response.raise_for_status()
                report.results = _parse_openalex([response.json()])
                report.total = len(report.results)
                return _finish(report, started)

            filters: list[str] = []
            author_filter_applied = False
            if spec.author_id:
                filters.append(f"authorships.author.id:{spec.author_id}")
                author_filter_applied = True
            elif spec.author:
                author_response = session.get(
                    "https://api.openalex.org/authors",
                    params={"search": spec.author, "per_page": 5, **params},
                    timeout=_TIMEOUT,
                )
                if author_response.status_code == 200:
                    candidates = author_response.json().get("results", [])
                    if candidates:
                        author = max(
                            candidates,
                            key=lambda item: (
                                item.get("cited_by_count", 0),
                                item.get("works_count", 0),
                            ),
                        )
                        filters.append(
                            "authorships.author.id:" + author.get("id", "").rsplit("/", 1)[-1]
                        )
                        author_filter_applied = True
                    else:
                        report.warnings.append(
                            "OpenAlex could not resolve the author profile; the author name was searched as text"
                        )
            if spec.date_from:
                filters.append(f"from_publication_date:{spec.date_from}")
            if spec.date_to:
                filters.append(f"to_publication_date:{spec.date_to}")
            if spec.publication_types:
                filters.append("type:" + "|".join(spec.publication_types))
            if spec.open_access_only:
                filters.append("is_oa:true")
            if spec.has_abstract is not None:
                filters.append(f"has_abstract:{str(spec.has_abstract).lower()}")
            if spec.min_citations is not None:
                filters.append(f"cited_by_count:>{max(0, spec.min_citations - 1)}")
            if spec.language:
                filters.append(f"language:{spec.language.lower()}")

            query_parts = [spec.query] if spec.query else []
            if spec.author and not author_filter_applied:
                query_parts.append(f'author:{quote_value(spec.author)}')
            if spec.venue:
                query_parts.append(f'journal:{quote_value(spec.venue)}')
            query, warnings = adapt_query(
                " AND ".join(query_parts), "openalex", exact=spec.exact
            )
            report.warnings.extend(warnings)
            if spec.fields_of_study:
                query = " AND ".join(
                    filter(None, [query, *[f'"{field}"' for field in spec.fields_of_study]])
                )
                report.warnings.append(
                    "OpenAlex field names were added to full-text search; use topic IDs for exact topic filtering"
                )
            if spec.category:
                report.warnings.append(
                    "OpenAlex does not expose the arXiv/preprint category filter"
                )
            if spec.recent_days:
                report.warnings.append(
                    "OpenAlex does not support recent_days; use date_from/date_to"
                )
            params.update({"per_page": spec.limit})
            if query:
                params["search.exact" if spec.exact else "search"] = query
            if filters:
                params["filter"] = ",".join(filters)
            sort_map = {
                "relevance": "relevance_score:desc",
                "publication_date": "publication_date:desc",
                "updated_date": "updated_date:desc",
                "cited_by_count": "cited_by_count:desc",
            }
            params["sort"] = sort_map[spec.sort]
            params["page"] = spec.offset // spec.limit + 1
            if spec.offset % spec.limit:
                report.warnings.append(
                    "OpenAlex offset is page-based; use offsets that are multiples of limit"
                )
            report.endpoint = "https://api.openalex.org/works"
            report.params = params
            response = session.get(report.endpoint, params=params, timeout=_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            report.results = _parse_openalex(payload.get("results", []))
            report.total = payload.get("meta", {}).get("count")
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        report.error = f"invalid OpenAlex response: {exc}"
    return _finish(report, started)


_S2_FIELDS = (
    "paperId,externalIds,url,title,abstract,venue,year,publicationDate,"
    "citationCount,isOpenAccess,openAccessPdf,fieldsOfStudy,publicationTypes,authors"
)


def _get_semantic(
    session: requests.Session,
    endpoint: str,
    params: dict[str, Any],
) -> requests.Response:
    for attempt in range(2):
        response = session.get(endpoint, params=params, timeout=_TIMEOUT)
        if response.status_code not in {429, 503} or attempt == 1:
            return response
        time.sleep(1.0)
    return response


def _parse_semantic(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for paper in papers:
        ids = paper.get("externalIds") or {}
        doi = ids.get("DOI") or ""
        arxiv_id = ids.get("ArXiv") or ""
        pmid = ids.get("PubMed") or ""
        oa = paper.get("openAccessPdf") or {}
        results.append({
            "title": paper.get("title") or "",
            "doi": doi,
            "identifier": doi or arxiv_id or pmid or paper.get("paperId") or "",
            "semantic_scholar_id": paper.get("paperId") or "",
            "arxiv_id": arxiv_id,
            "pmid": str(pmid),
            "url": paper.get("url") or "",
            "authors": [
                author.get("name", "") for author in (paper.get("authors") or [])[:10]
                if author.get("name")
            ],
            "year": paper.get("year") or "",
            "publication_date": paper.get("publicationDate") or "",
            "venue": paper.get("venue") or "",
            "type": ",".join(paper.get("publicationTypes") or []),
            "fields_of_study": paper.get("fieldsOfStudy") or [],
            "cited_by_count": paper.get("citationCount") or 0,
            "abstract": (paper.get("abstract") or "")[:2000],
            "is_oa": bool(paper.get("isOpenAccess")),
            "oa_url": oa.get("url") or "",
            "source": "semantic_scholar",
        })
    return results


def search_semantic_scholar(
    spec: SearchSpec, config: dict[str, Any]
) -> SourceReport:
    started = time.monotonic()
    report = SourceReport(source="semantic_scholar")
    api_key = (
        os.environ.get("S2_API_KEY")
        or config.get("semantic_scholar_api_key", "")
    )
    headers = {"x-api-key": api_key} if api_key else {}
    report.authenticated = bool(api_key)
    identifier_type, identifier = spec.identifier

    try:
        with _session(headers) as session:
            if identifier_type in {"doi", "arxiv"}:
                prefix = "DOI" if identifier_type == "doi" else "ARXIV"
                paper_id = quote(f"{prefix}:{identifier}", safe=":")
                report.endpoint = (
                    f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
                )
                report.params = {"fields": _S2_FIELDS}
                response = _get_semantic(
                    session, report.endpoint, report.params
                )
                if response.status_code == 404:
                    report.total = 0
                    return _finish(report, started)
                response.raise_for_status()
                report.results = _parse_semantic([response.json()])
                report.total = len(report.results)
                return _finish(report, started)

            query, warnings = adapt_query(
                spec.effective_query, "semantic_scholar", exact=spec.exact
            )
            report.warnings.extend(warnings)
            bulk = spec.advanced
            report.endpoint = (
                "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
                if bulk else
                "https://api.semanticscholar.org/graph/v1/paper/search"
            )
            params: dict[str, Any] = {
                "query": query,
                "fields": _S2_FIELDS,
                "limit": spec.limit,
            }
            if not bulk:
                params["offset"] = spec.offset
            elif spec.offset:
                report.warnings.append(
                    "Semantic Scholar bulk search uses tokens; offset was ignored"
                )
            if spec.date_from or spec.date_to:
                params["publicationDateOrYear"] = (
                    f"{spec.date_from or ''}:{spec.date_to or ''}"
                )
            if spec.fields_of_study:
                params["fieldsOfStudy"] = ",".join(spec.fields_of_study)
            if spec.publication_types:
                params["publicationTypes"] = ",".join(spec.publication_types)
            if spec.open_access_only:
                params["openAccessPdf"] = ""
            if spec.min_citations is not None:
                params["minCitationCount"] = spec.min_citations
            if spec.venue:
                params["venue"] = spec.venue
            if spec.language:
                report.warnings.append(
                    "Semantic Scholar search does not expose a language filter"
                )
            if spec.category:
                report.warnings.append(
                    "Semantic Scholar does not expose the arXiv/preprint category filter"
                )
            if spec.recent_days:
                report.warnings.append(
                    "Semantic Scholar does not support recent_days; use date_from/date_to"
                )
            if bulk and spec.sort != "relevance":
                sort_field = {
                    "publication_date": "publicationDate",
                    "updated_date": "publicationDate",
                    "cited_by_count": "citationCount",
                }[spec.sort]
                params["sort"] = f"{sort_field}:desc"
            elif not bulk and spec.sort != "relevance":
                report.warnings.append(
                    "Semantic Scholar relevance endpoint cannot sort; merged results are sorted locally"
                )
            report.params = params
            response = _get_semantic(session, report.endpoint, params)
            response.raise_for_status()
            payload = response.json()
            report.results = _parse_semantic(payload.get("data", []))
            report.total = payload.get("total")
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        report.error = f"invalid Semantic Scholar response: {exc}"
    return _finish(report, started)


def _parse_crossref(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        doi = item.get("DOI") or ""
        title_values = item.get("title") or []
        venue_values = item.get("container-title") or []
        publication_date = (
            item.get("published-online") or item.get("published-print")
            or item.get("published") or {}
        )
        date_parts = publication_date.get("date-parts") or [[]]
        parts = date_parts[0] if date_parts else []
        date_value = "-".join(
            [f"{parts[0]:04d}"]
            + [f"{part:02d}" for part in parts[1:3]]
        ) if parts else ""
        links = item.get("link") or []
        pdf_url = next(
            (
                link.get("URL", "") for link in links
                if link.get("content-type") == "application/pdf"
            ),
            "",
        )
        abstract = re.sub(r"<[^>]+>", "", item.get("abstract") or "")[:2000]
        results.append({
            "title": title_values[0] if title_values else "",
            "doi": doi,
            "identifier": doi,
            "url": f"https://doi.org/{doi}" if doi else "",
            "authors": [
                " ".join(
                    filter(None, [author.get("given"), author.get("family")])
                )
                for author in (item.get("author") or [])[:10]
            ],
            "year": parts[0] if parts else "",
            "publication_date": date_value,
            "venue": venue_values[0] if venue_values else "",
            "type": item.get("type") or "",
            "cited_by_count": item.get("is-referenced-by-count") or 0,
            "abstract": abstract,
            "is_oa": False,
            "oa_url": "",
            "has_full_text_link": bool(pdf_url),
            "full_text_url": pdf_url,
            "source": "crossref",
        })
    return results


def search_crossref(spec: SearchSpec, config: dict[str, Any]) -> SourceReport:
    started = time.monotonic()
    report = SourceReport(source="crossref")
    contact = config.get("email", "")
    identifier_type, identifier = spec.identifier
    params: dict[str, Any] = {}
    if valid_contact_email(contact):
        params["mailto"] = contact
    try:
        with _session() as session:
            if identifier_type == "doi":
                report.endpoint = (
                    "https://api.crossref.org/works/" + quote(identifier, safe="")
                )
                report.params = params
                response = session.get(report.endpoint, params=params, timeout=_TIMEOUT)
                if response.status_code == 404:
                    report.total = 0
                    return _finish(report, started)
                response.raise_for_status()
                report.results = _parse_crossref([response.json().get("message", {})])
                report.total = len(report.results)
                return _finish(report, started)

            query, warnings = adapt_query(
                spec.effective_query, "crossref", exact=spec.exact
            )
            report.warnings.extend(warnings)
            if spec.fields_of_study:
                query = " AND ".join(
                    filter(None, [query, *[f'"{field}"' for field in spec.fields_of_study]])
                )
                report.warnings.append(
                    "Crossref fields_of_study values were added to bibliographic text search"
                )
            fields = extract_fields(spec.effective_query)
            params.update({
                "query.bibliographic": query,
                "rows": spec.limit,
                "offset": spec.offset,
                "select": (
                    "DOI,title,author,published,published-print,published-online,"
                    "is-referenced-by-count,abstract,link,container-title,type"
                ),
            })
            if fields.get("author"):
                params["query.author"] = " ".join(fields["author"])
            if fields.get("journal") or spec.venue:
                params["query.container-title"] = (
                    spec.venue or " ".join(fields.get("journal", []))
                )
            filters: list[str] = []
            if spec.date_from:
                filters.append(f"from-pub-date:{spec.date_from}")
            if spec.date_to:
                filters.append(f"until-pub-date:{spec.date_to}")
            for publication_type in spec.publication_types:
                filters.append(f"type:{publication_type}")
            if spec.has_abstract is True:
                filters.append("has-abstract:true")
            if spec.has_abstract is False:
                filters.append("has-abstract:false")
            if spec.open_access_only:
                report.warnings.append(
                    "Crossref cannot prove OA status; has-full-text is not treated as equivalent"
                )
            if spec.language:
                report.warnings.append("Crossref search does not expose a language filter")
            if spec.category:
                report.warnings.append(
                    "Crossref does not expose the arXiv/preprint category filter"
                )
            if spec.recent_days:
                report.warnings.append(
                    "Crossref does not support recent_days; use date_from/date_to"
                )
            if filters:
                params["filter"] = ",".join(filters)
            if spec.sort != "relevance":
                params["sort"] = {
                    "publication_date": "published",
                    "updated_date": "updated",
                    "cited_by_count": "is-referenced-by-count",
                }[spec.sort]
                params["order"] = "desc"
            report.endpoint = "https://api.crossref.org/works"
            report.params = params
            response = session.get(report.endpoint, params=params, timeout=_TIMEOUT)
            response.raise_for_status()
            payload = response.json().get("message", {})
            report.results = _parse_crossref(payload.get("items", []))
            report.total = payload.get("total-results")
    except requests.RequestException as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        report.error = f"invalid Crossref response: {exc}"
    return _finish(report, started)
