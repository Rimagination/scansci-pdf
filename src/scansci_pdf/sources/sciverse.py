"""Sciverse API client: agentic search and full-text content retrieval.

Docs:
- https://sciverse.space/docs/sciverse/api/agentic-search
- https://sciverse.space/docs/sciverse/api/content

The API turns a natural-language research question into citable evidence
chunks (``hits``), each carrying ``doc_id`` / ``chunk_id`` / ``offset`` so a
caller can quote it and read the surrounding full text through ``/content``.
It is a semantic *evidence* search, not a field-filtered metadata export; use
OpenAlex/Crossref (or Sciverse ``/meta-search``) when exact DOI filtering is
needed.

Both endpoints require a bearer token from https://sciverse.space/tokens.
Set it with ``scansci_pdf_config(key="sciverse_api_key", value="...")``.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

import requests

from ..config import load_config
from ..network import _get_session, request_timeout

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.sciverse.space"
TOKEN_URL = "https://sciverse.space/tokens"
MAX_QUERY_CHARS = 4096
MAX_TOP_K = 100
MAX_SUB_QUERIES = 4
# meta-search caps total_count at this value; it is an upper bound, not exact.
META_TOTAL_CAP = 10000
# Fields needed to make an evidence hit download-ready without re-querying.
_METADATA_FIELDS = (
    "doc_id", "doi", "unique_id", "title", "publication_published_year",
    "citation_count", "author", "publication_venue_name_unified",
    "publication_venue_type", "primary_topic", "language",
    "access_is_oa", "access_oa_url", "influential_citation_count", "fwci",
)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3

# Error-code → actionable next step. Unknown codes fall back to the HTTP hint.
_ERROR_ACTIONS = {
    "INVALID_REQUEST": "check query/top_k/offset and retry",
    "SCOPE_TOO_LARGE": "reduce doc_id scope (max 1000 unique ids)",
    "EMPTY_RESULT": "broaden the query or relax filters",
    "UNAUTHORIZED": "check sciverse_api_key (create one at https://sciverse.space/tokens)",
    "RATE_LIMITED": "wait for the rate-limit window or daily quota reset, then retry",
    "INTERNAL_ERROR": "retry with exponential backoff",
    "UPSTREAM_UNAVAILABLE": "retry with exponential backoff",
    "NOT_FOUND": "the doc_id has no retrievable full text",
    "METHOD_NOT_ALLOWED": "use the documented HTTP method",
}


class SciverseError(RuntimeError):
    """Structured Sciverse API failure (status/code/retryability preserved)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        code: str = "",
        retryable: bool = False,
        action: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.action = action or _ERROR_ACTIONS.get(code.upper(), "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "source": "sciverse",
            "error": str(self),
            "error_code": self.code,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "action": self.action,
        }


def is_configured(config: dict[str, Any] | None = None) -> bool:
    """True when Sciverse is enabled and an API key is present."""
    cfg = config if config is not None else load_config()
    if not cfg.get("sciverse_enabled", True):
        return False
    return bool(str(cfg.get("sciverse_api_key") or "").strip())


def key_preview(key: str | None) -> str:
    """Display-safe token preview (never leaks the full secret)."""
    value = str(key or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def _base_url(config: dict[str, Any]) -> str:
    return str(config.get("sciverse_base_url") or DEFAULT_BASE_URL).rstrip("/")


def _timeout(config: dict[str, Any]) -> tuple[int, int]:
    connect, read = request_timeout(config)
    try:
        configured = int(config.get("sciverse_timeout_seconds", 0) or 0)
    except (TypeError, ValueError):
        configured = 0
    return (connect or 5, max(read, configured or 0))


def _parse_error(response: requests.Response) -> tuple[str, str]:
    """Extract (code, message) from a Sciverse error body, tolerantly."""
    message = (getattr(response, "text", "") or "")[:300].strip()
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("detail") or message)
            code = str(err.get("code") or err.get("type") or data.get("code") or "")
        elif isinstance(err, str):
            message = err
            code = str(data.get("code") or "")
        else:
            message = str(data.get("message") or data.get("detail") or message)
            code = str(data.get("code") or data.get("error_code") or "")
        return code, message or f"HTTP {response.status_code}"
    return "", message or f"HTTP {response.status_code}"


def _retry_delay(response: requests.Response, attempt: int) -> float:
    """Bounded backoff; honor a short Retry-After but never sleep for hours."""
    try:
        retry_after = float(response.headers.get("Retry-After", ""))
        if 0 < retry_after <= 10:
            return retry_after
    except (TypeError, ValueError):
        pass
    return 1.0 * (2 ** attempt)


def _request(
    method: str,
    path: str,
    *,
    config: dict[str, Any],
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> requests.Response:
    """Authorized request with bounded retries; returns a 2xx response."""
    api_key = str(config.get("sciverse_api_key") or "").strip()
    if not api_key:
        raise SciverseError(
            "Sciverse API key is not configured",
            code="UNAUTHORIZED",
        )

    url = _base_url(config) + path
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": accept,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    last_error: SciverseError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = _get_session(config).request(
                method,
                url,
                headers=headers,
                params=params,
                json=body,
                timeout=_timeout(config),
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            last_error = SciverseError(
                f"Sciverse network error: {exc}",
                retryable=True,
                action="check network access or configure network_proxy, then retry",
            )
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise last_error

        if 200 <= response.status_code < 300:
            return response

        code, message = _parse_error(response)
        retryable = response.status_code in _RETRY_STATUS
        last_error = SciverseError(
            message,
            status_code=response.status_code,
            code=code,
            retryable=retryable,
        )
        if retryable and attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_retry_delay(response, attempt))
            continue
        raise last_error

    raise last_error or SciverseError("Sciverse request failed")


def _request_json(
    method: str,
    path: str,
    *,
    config: dict[str, Any],
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = _request(method, path, config=config, params=params, body=body)
    try:
        payload = response.json()
    except ValueError as exc:
        raise SciverseError(
            f"Sciverse returned a non-JSON response: {exc}",
            status_code=response.status_code,
            retryable=False,
        ) from None
    if not isinstance(payload, dict):
        raise SciverseError(
            f"Sciverse returned an unexpected {type(payload).__name__} payload",
            status_code=response.status_code,
        )
    return payload


def build_filters(
    *,
    lang: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    venue: str = "",
    venue_type: str = "",
    author: str | Iterable[str] | None = None,
    topic: str | Iterable[str] | None = None,
    topic_domain: str = "",
    topic_logic: str = "or",
    doc_ids: Iterable[str] | None = None,
    citation_min: int | None = None,
    citation_max: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an agentic-search ``filters`` object from common fields.

    Fields are ANDed by the API; list values within one field are ORed.
    ``doc_id`` is the only hard scope (missing metadata is otherwise not
    excluded).
    """
    filters: dict[str, Any] = {}

    if lang:
        filters["lang"] = lang
    if venue:
        filters["publication_venue_name_unified"] = venue
    if venue_type:
        filters["publication_venue_type"] = venue_type

    if year_from is not None or year_to is not None:
        year_filter: dict[str, int] = {}
        if year_from is not None:
            year_filter["gte"] = int(year_from)
        if year_to is not None:
            year_filter["lte"] = int(year_to)
        filters["publication_published_year"] = year_filter

    if citation_min is not None or citation_max is not None:
        citation_filter: dict[str, int] = {}
        if citation_min is not None:
            citation_filter["gte"] = int(citation_min)
        if citation_max is not None:
            citation_filter["lte"] = int(citation_max)
        filters["citation_count"] = citation_filter

    if author:
        filters["author"] = [author] if isinstance(author, str) else list(author)

    dimensions: dict[str, Any] = {}
    if topic:
        dimensions["primary_topic"] = [topic] if isinstance(topic, str) else list(topic)
    if topic_domain:
        dimensions["primary_topic_domain"] = topic_domain
    if dimensions:
        filters["topics"] = {
            "logic": "and" if str(topic_logic).lower() == "and" else "or",
            "dimensions": dimensions,
        }

    if doc_ids:
        raw_ids = [doc_ids] if isinstance(doc_ids, str) else doc_ids
        ids = [str(doc_id).strip() for doc_id in raw_ids if str(doc_id).strip()]
        if ids:
            filters["doc_id"] = ids

    if extra:
        filters.update(extra)
    return filters


def agentic_search(
    query: str,
    *,
    config: dict[str, Any] | None = None,
    top_k: int = 10,
    sub_queries: int = 0,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Sciverse agentic-search and return the raw ``{"hits": [...]}`` payload.

    Args:
        query: Natural-language research question (max 4096 chars).
        config: scansci-pdf config (loads from disk when omitted).
        top_k: Chunks to return, 1–100. Without ``sub_queries`` the server
            fusion pool effectively returns at most ~50.
        sub_queries: Query-rewrite count, 0–4 (more recall, more latency).
        filters: Filter object (see :func:`build_filters`).
    """
    cfg = config if config is not None else load_config()
    text = (query or "").strip()
    if not text:
        raise SciverseError("query must not be empty", code="INVALID_REQUEST")
    if len(text) > MAX_QUERY_CHARS:
        raise SciverseError(
            f"query is {len(text)} chars; the API accepts at most {MAX_QUERY_CHARS}",
            code="INVALID_REQUEST",
        )

    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 10
    top_k = max(1, min(MAX_TOP_K, top_k))
    try:
        sub_queries = int(sub_queries)
    except (TypeError, ValueError):
        sub_queries = 0
    sub_queries = max(0, min(MAX_SUB_QUERIES, sub_queries))

    body: dict[str, Any] = {"query": text, "top_k": top_k, "sub_queries": sub_queries}
    if filters:
        body["filters"] = filters

    return _request_json("POST", "/agentic-search", config=cfg, body=body)


def content(
    doc_id: str,
    *,
    config: dict[str, Any] | None = None,
    offset: int | None = None,
    limit: int = 700,
) -> dict[str, Any]:
    """Read a full-text segment for ``doc_id`` (``/content``).

    Returns ``{text, chars_returned, next_offset, more}``. ``limit`` only
    takes effect when ``offset`` is provided, matching the API contract.
    """
    cfg = config if config is not None else load_config()
    doc = str(doc_id or "").strip()
    if not doc:
        raise SciverseError("doc_id must not be empty", code="INVALID_REQUEST")

    params: dict[str, Any] = {"doc_id": doc}
    if offset is not None:
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            raise SciverseError("offset must be an integer >= 0", code="INVALID_REQUEST") from None
        if offset < 0:
            raise SciverseError("offset must be an integer >= 0", code="INVALID_REQUEST")
        params["offset"] = offset
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 700
        params["limit"] = max(1, limit)

    payload = _request_json("GET", "/content", config=cfg, params=params)
    # The live API sometimes omits chars_returned; keep the documented shape
    # so callers can always page by length without special-casing None.
    if payload.get("chars_returned") is None and isinstance(payload.get("text"), str):
        payload["chars_returned"] = len(payload["text"])
    return payload


def _filter_operator(operator: str) -> str:
    op = str(operator or "EQ").upper()
    return op if op.startswith("FILTER_OP_") else f"FILTER_OP_{op}"


def build_meta_filters(
    *,
    year_from: int | None = None,
    year_to: int | None = None,
    lang: str = "",
    venue: str = "",
    venue_type: str = "",
    author: str = "",
    topic: str | Iterable[str] | None = None,
    topic_domain: str = "",
    doi: str | Iterable[str] | None = None,
    doc_ids: Iterable[str] | None = None,
    citation_min: int | None = None,
    citation_max: int | None = None,
    extra: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build ``meta-search`` FilterItems from common fields.

    Field names follow the documented meta-search catalog; operators use the
    API's ``FILTER_OP_*`` enum. Filters combine with AND.
    """
    filters: list[dict[str, Any]] = []

    def add(field: str, value: Any, operator: str = "EQ") -> None:
        filters.append({
            "field": field,
            "operator": _filter_operator(operator),
            "value": value,
        })

    if year_from is not None:
        add("publication_published_year", int(year_from), "GTE")
    if year_to is not None:
        add("publication_published_year", int(year_to), "LTE")
    if citation_min is not None:
        add("citation_count", int(citation_min), "GTE")
    if citation_max is not None:
        add("citation_count", int(citation_max), "LTE")
    if lang:
        add("language", lang, "EQ")
    if venue:
        add("publication_venue_name_unified", venue, "MATCH_PHRASE")
    if venue_type:
        add("publication_venue_type", venue_type, "EQ")
    if author:
        # Documented as a fuzzy author-name match (author.name under the hood).
        add("author", author, "MATCH")

    raw_topics = topic if isinstance(topic, (list, tuple, set)) else ([topic] if topic else [])
    topics = [
        str(item).strip()
        for item in raw_topics
        if item is not None and str(item).strip()
    ]
    if topics:
        add("primary_topic.display_name", topics[0] if len(topics) == 1 else topics,
            "EQ" if len(topics) == 1 else "IN")
    if topic_domain:
        add("primary_topic.domain.display_name", topic_domain, "EQ")

    raw_dois = doi if isinstance(doi, (list, tuple, set)) else ([doi] if doi else [])
    dois = [
        str(item).strip()
        for item in raw_dois
        if item is not None and str(item).strip()
    ]
    if dois:
        add("doi", dois[0] if len(dois) == 1 else dois, "EQ" if len(dois) == 1 else "IN")

    raw_ids = [doc_ids] if isinstance(doc_ids, str) else (doc_ids or [])
    ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    if ids:
        add("doc_id", ids, "IN")

    if extra:
        for item in extra:
            if isinstance(item, dict) and item.get("field"):
                normalized = dict(item)
                normalized["operator"] = _filter_operator(str(item.get("operator") or "EQ"))
                filters.append(normalized)
    return filters


def build_meta_sort(
    sort: str | Iterable[dict[str, Any]] | None,
    *,
    order: str = "desc",
) -> list[dict[str, Any]] | None:
    """Normalize a sort spec to ``[{field, order}]`` (Meta-search SortItem)."""
    if not sort:
        return None
    if isinstance(sort, str):
        field = sort.strip()
        sort_order = order
        if ":" in field:
            field, sort_order = field.split(":", 1)
        if not field:
            return None
        items = [{"field": field.strip(), "order": sort_order.strip()}]
    else:
        items = [dict(item) for item in sort if isinstance(item, dict) and item.get("field")]
    if not items:
        return None
    for item in items:
        raw = str(item.get("order") or "DESC").upper()
        if not raw.startswith("SORT_ORDER_"):
            raw = f"SORT_ORDER_{raw}"
        item["order"] = raw
    return items


def meta_search(
    query: str = "",
    *,
    config: dict[str, Any] | None = None,
    collection: str = "papers",
    filters: list[dict[str, Any]] | None = None,
    sort: list[dict[str, Any]] | None = None,
    fields: Iterable[str] | None = None,
    page: int = 1,
    page_size: int = 25,
    cursor: str | None = None,
    freshness_boost: str = "",
    impact_boost: str = "",
    language_affinity: str = "",
) -> dict[str, Any]:
    """Structured metadata search (``POST /meta-search``), raw response."""
    cfg = config if config is not None else load_config()
    collection_name = str(collection or "papers").lower()
    if collection_name not in ("papers", "authors", "sources"):
        raise SciverseError(
            "collection must be papers, authors, or sources",
            code="INVALID_REQUEST",
        )

    text = (query or "").strip()
    if collection_name == "papers" and not text and not filters:
        raise SciverseError(
            "query and filters must not both be empty",
            code="INVALID_REQUEST",
        )

    body: dict[str, Any] = {"collection": collection_name}
    if text:
        body["query"] = text
    if filters:
        body["filters"] = filters
    if sort:
        body["sort"] = sort
    raw_fields = [fields] if isinstance(fields, str) else (fields or [])
    projected = [str(field) for field in raw_fields if str(field).strip()]
    if projected:
        body["fields"] = projected

    if cursor:
        body["cursor"] = str(cursor)
    else:
        try:
            body["page"] = max(1, int(page))
        except (TypeError, ValueError):
            body["page"] = 1
        try:
            body["page_size"] = max(1, min(200, int(page_size)))
        except (TypeError, ValueError):
            body["page_size"] = 25

    for key, value in (
        ("freshness_boost", freshness_boost),
        ("impact_boost", impact_boost),
        ("language_affinity", language_affinity),
    ):
        normalized = str(value or "").upper()
        if normalized in ("NONE", "MILD", "STRONG"):
            body[key] = normalized

    payload = _request_json("POST", "/meta-search", config=cfg, body=body)
    total = payload.get("total_count")
    if (
        isinstance(total, (int, float))
        and not isinstance(total, bool)
        and total >= META_TOTAL_CAP
    ):
        payload["total_count_capped"] = True
    return payload


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce OpenAlex-backed numeric fields, which may arrive as floats."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def meta_search_results(
    payload: dict[str, Any],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Map meta-search rows to the unified paper-record shape."""
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []

    results: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        doi = str(row.get("doi") or "").strip()
        topic = row.get("primary_topic")
        if isinstance(topic, dict):
            topic_name = str(topic.get("display_name") or "")
            domain = topic.get("domain")
            domain_name = str(domain.get("display_name") or "") if isinstance(domain, dict) else ""
        else:
            topic_name = str(topic or "")
            domain_name = ""
        oa_url = str(row.get("access_oa_url") or "")
        is_oa = row.get("access_is_oa")
        results.append({
            "title": str(row.get("title") or ""),
            "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else "",
            "authors": _as_list(row.get("author"))[:5],
            "year": _hit_year(row),
            "cited_by_count": _as_int(row.get("citation_count")),
            "abstract": str(row.get("abstract") or "")[:500],
            "is_oa": bool(is_oa) if is_oa is not None else bool(oa_url),
            "oa_url": oa_url,
            "source": "sciverse-meta",
            "doc_id": str(row.get("doc_id") or ""),
            "unique_id": str(row.get("unique_id") or ""),
            "journal": str(row.get("publication_venue_name_unified") or ""),
            "venue_type": str(row.get("publication_venue_type") or ""),
            "primary_topic": topic_name,
            "primary_topic_domain": domain_name,
            "language": str(row.get("language") or ""),
            "influential_citation_count": _as_int(row.get("influential_citation_count")),
            "fwci": row.get("fwci"),
        })
    return results[:limit] if limit is not None else results


def _metadata_by_doc_id(
    doc_ids: Iterable[str],
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Fetch DOI/unique_id metadata for Sciverse full-text doc_ids."""
    ids = list(dict.fromkeys(
        str(doc_id).strip() for doc_id in doc_ids if str(doc_id).strip()
    ))[:200]
    if not ids:
        return {}
    payload = meta_search(
        config=config,
        filters=[{"field": "doc_id", "operator": "FILTER_OP_IN", "value": ids}],
        fields=_METADATA_FIELDS,
        page_size=min(200, len(ids)),
    )
    return {
        record["doc_id"]: record
        for record in meta_search_results(payload)
        if record.get("doc_id")
    }


def enrich_records_with_meta(
    records: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fill missing DOI/unique_id metadata on evidence records via doc_id.

    Scenario: unified paper search / download selection. Agentic hits carry a
    doc_id but no DOI; one doc_id-scoped meta-search call makes them
    download-ready. Standalone evidence search can skip this to save quota.
    """
    cfg = config if config is not None else load_config()
    missing = [
        record for record in records
        if record.get("doc_id") and not record.get("doi")
    ]
    if not missing:
        return records
    try:
        by_doc_id = _metadata_by_doc_id(
            (record["doc_id"] for record in missing), cfg
        )
    except SciverseError as exc:
        logger.warning("sciverse: doc_id metadata enrichment failed: %s", exc)
        return records
    for record in missing:
        metadata = by_doc_id.get(str(record.get("doc_id") or ""))
        if not metadata:
            continue
        for field in (
            "doi", "url", "unique_id", "journal", "venue_type", "year",
            "cited_by_count", "is_oa", "oa_url", "primary_topic",
            "primary_topic_domain", "language", "fwci",
            "influential_citation_count",
        ):
            if not record.get(field) and metadata.get(field):
                record[field] = metadata[field]
    return records


def enrich_hits_with_metadata(
    hits: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Annotate raw agentic hits with DOI/unique_id when the caller asks.

    Keeps the raw hit shape; adds ``doi``/``unique_id`` and fills
    ``publication_published_year``/``citation_count`` when missing.
    """
    cfg = config if config is not None else load_config()
    missing = [
        hit for hit in hits
        if hit.get("doc_id") and not hit.get("doi")
    ]
    if not missing:
        return hits
    try:
        by_doc_id = _metadata_by_doc_id(
            (hit["doc_id"] for hit in missing), cfg
        )
    except SciverseError as exc:
        logger.warning("sciverse: hit metadata enrichment failed: %s", exc)
        return hits
    for hit in missing:
        metadata = by_doc_id.get(str(hit.get("doc_id") or ""))
        if not metadata:
            continue
        if metadata.get("doi") and not hit.get("doi"):
            hit["doi"] = metadata["doi"]
        if metadata.get("unique_id") and not hit.get("unique_id"):
            hit["unique_id"] = metadata["unique_id"]
        if metadata.get("year") and not hit.get("publication_published_year"):
            hit["publication_published_year"] = metadata["year"]
        if metadata.get("cited_by_count") and not hit.get("citation_count"):
            hit["citation_count"] = metadata["cited_by_count"]
        if metadata.get("journal") and not hit.get("publication_venue_name_unified"):
            hit["publication_venue_name_unified"] = metadata["journal"]
    return hits


def meta_search_papers(
    query: str = "",
    *,
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    config: dict[str, Any] | None = None,
    filters: list[dict[str, Any]] | None = None,
    sort: str | Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Meta-search wrapper returning unified records (empty when unconfigured)."""
    cfg = config if config is not None else load_config()
    if not is_configured(cfg):
        return []

    built = build_meta_filters(year_from=year_from, year_to=year_to)
    if filters:
        built.extend(filters)
    if not (query or "").strip() and not built:
        return []

    payload = meta_search(
        query,
        config=cfg,
        filters=built,
        sort=build_meta_sort(sort),
        page_size=max(1, min(200, limit)),
    )
    return meta_search_results(payload, limit=limit)


def paper_relations(
    unique_id: str,
    *,
    relation: str = "REFERENCES",
    config: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    """Page through a paper's citations/references/related works."""
    cfg = config if config is not None else load_config()
    uid = str(unique_id or "").strip()
    if not uid:
        raise SciverseError("unique_id must not be empty", code="INVALID_REQUEST")
    # Accept a bare DOI as a convenience; the API expects paper:<doi>.
    if re.match(r"^10\.\d{4,9}/", uid):
        uid = f"paper:{uid}"
    rel = str(relation or "REFERENCES").upper()
    if rel not in ("CITATIONS", "REFERENCES", "RELATED_WORKS"):
        raise SciverseError(
            "relation must be CITATIONS, REFERENCES, or RELATED_WORKS",
            code="INVALID_REQUEST",
        )
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(200, int(page_size)))
    except (TypeError, ValueError):
        page_size = 25
    return _request_json(
        "POST",
        "/meta-paper-relations",
        config=cfg,
        body={
            "unique_id": uid,
            "relation": rel,
            "page": page,
            "page_size": page_size,
        },
    )


def meta_catalog(
    *,
    collection: str = "papers",
    include_sample_values: bool = False,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the runtime field/operator catalog for meta-search."""
    cfg = config if config is not None else load_config()
    collection_name = str(collection or "papers").lower()
    if collection_name not in ("papers", "authors", "sources"):
        raise SciverseError(
            "collection must be papers, authors, or sources",
            code="INVALID_REQUEST",
        )
    params: dict[str, Any] = {"collection": collection_name}
    if include_sample_values:
        params["include_sample_values"] = "true"
    return _request_json("GET", "/meta-catalog", config=cfg, params=params)


def validate_key(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the configured token with the lightest documented endpoint.

    ``meta-catalog`` is a small GET (~120ms) that exercises auth and field
    permissions without consuming agentic-search results.
    """
    cfg = config if config is not None else load_config()
    if not is_configured(cfg):
        return {
            "status": "not_configured",
            "message": "sciverse_api_key is not configured",
            "action": f"create a token at {TOKEN_URL} and run sciverse-setup",
        }
    try:
        payload = meta_catalog(collection="papers", config=cfg)
    except SciverseError as exc:
        if exc.status_code == 401:
            status = "invalid"
        elif exc.status_code == 403:
            status = "forbidden"
        elif exc.status_code == 429:
            status = "rate_limited"
        else:
            status = "error"
        return {
            "status": status,
            "message": str(exc),
            "status_code": exc.status_code,
            "code": exc.code,
            "retryable": exc.retryable,
            "action": exc.action,
        }
    fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
    return {
        "status": "ok",
        "message": "Sciverse token is valid",
        "fields": len(fields),
        "contract_version": payload.get("contract_version"),
    }


def _validate_resource_name(file_name: str) -> str:
    name = str(file_name or "").strip()
    if not name:
        raise SciverseError("file_name must not be empty", code="INVALID_REQUEST")
    if (
        "\\" in name
        or name.startswith("/")
        or "://" in name
        or ".." in name.split("/")
    ):
        raise SciverseError(
            "file_name must be a relative Sciverse resource path",
            code="INVALID_REQUEST",
        )
    return name


def _resource_filename(data: dict[str, Any]) -> str:
    disposition = str(data.get("content_disposition") or "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.I)
    name = unquote(match.group(1)) if match else Path(str(data.get("file_name") or "")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "resource"


def resource(
    file_name: str,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Download a figure/table/attachment binary (``GET /resource``)."""
    cfg = config if config is not None else load_config()
    name = _validate_resource_name(file_name)
    response = _request(
        "GET",
        "/resource",
        config=cfg,
        params={"file_name": name},
        accept="*/*",
    )
    content = response.content or b""
    if not content:
        raise SciverseError(
            "Sciverse returned an empty resource",
            status_code=response.status_code,
            code="NOT_FOUND",
        )
    content_type = response.headers.get("Content-Type", "application/octet-stream")
    if content_type.lower().startswith("text/html") and not name.lower().endswith((".html", ".htm")):
        raise SciverseError(
            "Sciverse returned HTML instead of a binary resource",
            status_code=response.status_code,
            code="NOT_FOUND",
        )
    return {
        "file_name": name,
        "content": content,
        "content_type": content_type,
        "content_disposition": response.headers.get("Content-Disposition", ""),
        "size": len(content),
    }


def save_resource(
    file_name: str,
    output_path: str | Path = "",
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Download a resource and write it to disk; returns metadata + path."""
    cfg = config if config is not None else load_config()
    data = resource(file_name, config=cfg)
    filename = _resource_filename(data)

    if output_path:
        target = Path(output_path).expanduser()
        if target.exists() and target.is_dir():
            target = target / filename
        elif not target.suffix:
            target = target / filename
    else:
        target = Path(cfg.get("output_dir") or ".") / "sciverse_resources" / filename
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data["content"])
    except OSError as exc:
        raise SciverseError(
            f"Failed to write Sciverse resource to {target}: {exc}",
            code="RESOURCE_WRITE_FAILED",
        ) from None
    return {
        "file_name": data["file_name"],
        "path": str(target),
        "content_type": data["content_type"],
        "size": data["size"],
    }


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("display_name") or "").strip()
            else:
                name = str(item).strip()
            if name:
                names.append(name)
        return names
    return []


def _hit_year(hit: dict[str, Any]) -> Any:
    year = hit.get("publication_published_year")
    if isinstance(year, (int, float)) and not isinstance(year, bool):
        return int(year)
    if isinstance(year, str):
        try:
            return int(float(year.strip()))
        except (TypeError, ValueError):
            pass
    date = str(hit.get("publication_published_date") or "")
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return ""


def _evidence(hit: dict[str, Any], *, chunk_chars: int = 400) -> dict[str, Any]:
    """Citation-safe excerpt: keep doc_id/chunk_id/offset/page_no."""
    chunk = str(hit.get("chunk") or "")
    if chunk_chars and len(chunk) > chunk_chars:
        chunk = chunk[:chunk_chars].rstrip() + "…"
    return {
        "chunk_id": str(hit.get("chunk_id") or ""),
        "chunk": chunk,
        "offset": hit.get("offset"),
        "page_no": hit.get("page_no"),
        "score": hit.get("score"),
    }


def _hit_key(hit: dict[str, Any]) -> str:
    doc_id = str(hit.get("doc_id") or "").strip()
    if doc_id:
        return f"doc:{doc_id}"
    doi = str(hit.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    title = str(hit.get("title") or "").strip().lower()
    return f"title:{title}" if title else f"chunk:{hit.get('chunk_id') or id(hit)}"


def to_search_results(
    payload: dict[str, Any],
    *,
    limit: int | None = None,
    max_evidence: int = 2,
) -> list[dict[str, Any]]:
    """Collapse chunk hits into one unified paper record per document.

    Records follow the shape used by :mod:`scansci_pdf.search` so Sciverse can
    be merged with OpenAlex/Crossref/S2 results. Up to ``max_evidence`` chunks
    per document are kept under ``evidence``; ``snippet`` is the top chunk.
    """
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        return []

    grouped: dict[str, dict[str, Any]] = {}
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        key = _hit_key(hit)
        evidence = _evidence(hit)
        existing = grouped.get(key)
        if existing is None:
            doi = str(hit.get("doi") or "").strip()
            year = _hit_year(hit)
            record: dict[str, Any] = {
                "title": str(hit.get("title") or ""),
                "doi": doi,
                "url": f"https://doi.org/{doi}" if doi else "",
                "authors": _as_list(hit.get("author"))[:5],
                "year": year,
                "cited_by_count": _as_int(hit.get("citation_count")),
                "abstract": str(hit.get("abstract") or "")[:500],
                "is_oa": False,
                "oa_url": "",
                "source": "sciverse",
                "doc_id": str(hit.get("doc_id") or ""),
                "journal": str(hit.get("publication_venue_name_unified") or ""),
                "venue_type": str(hit.get("publication_venue_type") or ""),
                "primary_topic": str(hit.get("primary_topic") or ""),
                "primary_topic_domain": str(hit.get("primary_topic_domain") or ""),
                "score": hit.get("score"),
                "snippet": evidence["chunk"],
                "evidence": [evidence],
            }
            grouped[key] = record
        else:
            if len(existing["evidence"]) < max_evidence:
                existing["evidence"].append(evidence)
            if not existing.get("abstract") and hit.get("abstract"):
                existing["abstract"] = str(hit["abstract"])[:500]

    results = list(grouped.values())
    return results[:limit] if limit is not None else results


def search_sciverse(
    query: str,
    *,
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    config: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    sub_queries: int | None = None,
    enrich_dois: bool = False,
) -> list[dict[str, Any]]:
    """Search Sciverse and return unified paper records (empty when unconfigured).

    ``enrich_dois`` is scenario-specific: unified download-oriented search
    enables it (one extra doc_id-scoped meta-search), while raw evidence
    retrieval leaves it off by default.
    """
    cfg = config if config is not None else load_config()
    if not is_configured(cfg):
        return []
    if sub_queries is None:
        try:
            sub_queries = int(cfg.get("sciverse_sub_queries", 0) or 0)
        except (TypeError, ValueError):
            sub_queries = 0

    merged_filters = build_filters(year_from=year_from, year_to=year_to)
    if filters:
        merged_filters.update(filters)

    # Hits are chunks, so ask for a wider candidate pool than the desired
    # number of documents; the documented per-request ceiling is 100.
    top_k = min(MAX_TOP_K, max(limit * 3, limit))
    payload = agentic_search(
        query,
        config=cfg,
        top_k=top_k,
        sub_queries=sub_queries,
        filters=merged_filters,
    )
    results = to_search_results(payload, limit=limit)
    if enrich_dois:
        results = enrich_records_with_meta(results, cfg)
    return results


__all__ = [
    "DEFAULT_BASE_URL",
    "TOKEN_URL",
    "SciverseError",
    "agentic_search",
    "build_filters",
    "build_meta_filters",
    "build_meta_sort",
    "content",
    "is_configured",
    "key_preview",
    "meta_catalog",
    "meta_search",
    "meta_search_papers",
    "meta_search_results",
    "paper_relations",
    "resource",
    "save_resource",
    "search_sciverse",
    "to_search_results",
    "validate_key",
]
