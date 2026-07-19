"""Advanced multi-database literature search orchestration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from .config import load_config
from .search_provider_arxiv import search_arxiv
from .search_providers_access import (
    enrich_unpaywall,
    search_core,
    search_unpaywall,
)
from .search_providers_biomedical import (
    search_biorxiv,
    search_medrxiv,
    search_pmc,
    search_pubmed,
)
from .search_providers_index import (
    search_crossref,
    search_openalex,
    search_semantic_scholar,
)
from .search_query import (
    SearchSpec,
    SourceReport,
    deduplicate_results,
    sort_results,
)

Provider = Callable[[SearchSpec, dict[str, Any]], SourceReport]

_PROVIDERS: dict[str, Provider] = {
    "pubmed": search_pubmed,
    "pmc": search_pmc,
    "biorxiv": search_biorxiv,
    "medrxiv": search_medrxiv,
    "arxiv": search_arxiv,
    "openalex": search_openalex,
    "crossref": search_crossref,
    "semantic_scholar": search_semantic_scholar,
    "core": search_core,
    "unpaywall": search_unpaywall,
}


def _route_sources(spec: SearchSpec) -> list[str]:
    if spec.sources:
        return spec.sources
    identifier_type, _ = spec.identifier
    if identifier_type == "doi":
        return ["crossref", "semantic_scholar", "unpaywall"]
    if identifier_type == "arxiv":
        return ["arxiv", "semantic_scholar", "openalex"]
    # Broad default follows paper-lookup's comprehensive-search contract.
    return ["pubmed", "openalex", "semantic_scholar", "crossref"]


def _validate_retrieval(spec: SearchSpec, sources: list[str]) -> None:
    if spec.effective_query:
        return
    if spec.author_id:
        return
    browse_sources = {"biorxiv", "medrxiv"}
    if (
        set(sources).issubset(browse_sources)
        and (spec.date_from or spec.date_to or spec.recent_days)
    ):
        return
    raise ValueError(
        "query is required unless author/author_id is set, or bioRxiv/medRxiv "
        "is browsed with date_from/date_to/recent_days"
    )


def search_papers_detailed(
    query: str = "",
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str | None = None,
    *,
    sources: list[str] | str | None = None,
    query_mode: str = "auto",
    exact: bool = False,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    author: str | None = None,
    author_id: str | None = None,
    publication_types: list[str] | str | None = None,
    fields_of_study: list[str] | str | None = None,
    venue: str | None = None,
    category: str | None = None,
    open_access_only: bool = False,
    has_abstract: bool | None = None,
    min_citations: int | None = None,
    language: str | None = None,
    recent_days: int | None = None,
    enrich_open_access: bool = False,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a bounded, auditable search across selected literature databases."""
    spec = SearchSpec(
        query=query,
        sources=sources,  # type: ignore[arg-type]
        query_mode=query_mode,
        exact=exact,
        limit=limit,
        offset=offset,
        year_from=year_from,
        year_to=year_to,
        date_from=date_from,
        date_to=date_to,
        sort=sort or "relevance",
        author=author,
        author_id=author_id,
        publication_types=publication_types,  # type: ignore[arg-type]
        fields_of_study=fields_of_study,  # type: ignore[arg-type]
        venue=venue,
        category=category,
        open_access_only=open_access_only,
        has_abstract=has_abstract,
        min_citations=min_citations,
        language=language,
        recent_days=recent_days,
        enrich_open_access=enrich_open_access,
    )
    selected_sources = _route_sources(spec)
    _validate_retrieval(spec, selected_sources)
    runtime_config = config if config is not None else load_config()

    reports_by_source: dict[str, SourceReport] = {}
    worker_count = max(1, min(4, len(selected_sources)))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="scansci-search",
    ) as pool:
        futures = {
            pool.submit(_PROVIDERS[source], spec, runtime_config): source
            for source in selected_sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                reports_by_source[source] = future.result()
            except Exception as exc:
                reports_by_source[source] = SourceReport(
                    source=source,
                    error=f"{type(exc).__name__}: {exc}",
                )

    reports = [
        reports_by_source.get(source, SourceReport(source=source))
        for source in selected_sources
    ]
    raw_results = [
        result
        for report in reports
        for result in report.results
    ]

    needs_enrichment = (
        spec.enrich_open_access or spec.open_access_only
    ) and spec.identifier[0] != "doi"
    if needs_enrichment:
        dois = [result.get("doi", "") for result in raw_results]
        oa_report = enrich_unpaywall(dois, runtime_config, max_items=min(10, spec.limit))
        reports.append(oa_report)
        raw_results.extend(oa_report.results)

    merged = deduplicate_results(raw_results)
    if spec.open_access_only:
        merged = [item for item in merged if item.get("is_oa")]
    if spec.has_abstract is not None:
        merged = [
            item for item in merged
            if (
                item.get("has_abstract")
                if item.get("has_abstract") is not None
                else bool(item.get("abstract"))
            ) is spec.has_abstract
        ]
    if spec.min_citations is not None:
        merged = [
            item for item in merged
            if (item.get("cited_by_count") or 0) >= spec.min_citations
        ]
    sort_results(merged, spec.sort)
    merged = merged[:spec.limit]

    warnings = [
        f"{report.source}: {warning}"
        for report in reports
        for warning in report.warnings
    ]
    errors = {
        report.source: report.error
        for report in reports
        if report.error
    }
    source_provenance = [report.provenance() for report in reports]
    retrieved_by_source = {
        report.source: len(report.results) for report in reports
    }
    total_by_source = {
        report.source: report.total for report in reports
        if report.total is not None
    }
    access_date = datetime.now(timezone.utc).date().isoformat()

    return {
        "results": merged,
        "retrieval": {
            "query": spec.query,
            "effective_query": spec.effective_query,
            "query_mode": "advanced" if spec.advanced else "plain",
            "scope": (
                "targeted_lookup" if spec.identifier[0]
                else "bounded_search"
            ),
            "sources_requested": selected_sources,
            "sources_queried": [
                report.source for report in reports if report.endpoint
            ],
            "access_date": access_date,
            "limit": spec.limit,
            "offset": spec.offset,
            "offset_scope": "per_source",
            "retrieved_by_source": retrieved_by_source,
            "total_by_source": total_by_source,
            "deduplicated_count": len(merged),
            "warnings": warnings,
            "errors": errors,
            "partial": bool(errors),
            "source_provenance": source_provenance,
        },
    }


def search_papers_advanced(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Compatibility helper returning only the result list."""
    return search_papers_detailed(*args, **kwargs)["results"]
