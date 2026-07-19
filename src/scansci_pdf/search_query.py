"""Shared contracts and query translation for advanced literature search."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .identifiers import normalize_arxiv_id

SUPPORTED_SOURCES = (
    "pubmed", "pmc", "biorxiv", "medrxiv", "arxiv",
    "openalex", "crossref", "semantic_scholar", "core", "unpaywall",
)
DISCOVERY_SOURCES = SUPPORTED_SOURCES[:-1]
FIELD_NAMES = (
    "title", "author", "abstract", "journal",
    "doi", "category", "mesh", "fulltext",
)

_DOI_RE = re.compile(r"(?i)^(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$")
_FIELD_RE = re.compile(
    r"(?i)\b(?P<field>title|author|abstract|journal|doi|category|mesh|fulltext):"
    r"(?P<value>\"(?:[^\"]|\\\")*\"|[^\s()]+)"
)
_ADVANCED_RE = re.compile(
    r'(?i)(?:\b(?:AND|OR|NOT)\b|[()\"*]|\b(?:'
    + "|".join(FIELD_NAMES)
    + r")\s*:)"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SOURCE_ALIASES = {
    "semantic-scholar": "semantic_scholar",
    "semantic scholar": "semantic_scholar",
    "s2": "semantic_scholar",
    "bio-rxiv": "biorxiv",
    "med-rxiv": "medrxiv",
    "open-alex": "openalex",
    "pubmed-central": "pmc",
}


@dataclass(slots=True)
class SearchSpec:
    query: str = ""
    sources: list[str] | None = None
    query_mode: str = "auto"
    exact: bool = False
    limit: int = 10
    offset: int = 0
    year_from: int | None = None
    year_to: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    sort: str = "relevance"
    author: str | None = None
    author_id: str | None = None
    publication_types: list[str] = field(default_factory=list)
    fields_of_study: list[str] = field(default_factory=list)
    venue: str | None = None
    category: str | None = None
    open_access_only: bool = False
    has_abstract: bool | None = None
    min_citations: int | None = None
    language: str | None = None
    recent_days: int | None = None
    enrich_open_access: bool = False

    def __post_init__(self) -> None:
        self.query = (self.query or "").strip()
        self.query_mode = (self.query_mode or "auto").strip().lower()
        self.sort = (self.sort or "relevance").strip().lower()
        if self.query_mode not in {"auto", "plain", "advanced"}:
            raise ValueError("query_mode must be auto, plain, or advanced")
        if self.sort not in {
            "relevance", "publication_date", "updated_date", "cited_by_count",
        }:
            raise ValueError(
                "sort must be relevance, publication_date, updated_date, or cited_by_count"
            )
        if not 1 <= int(self.limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        if int(self.offset) < 0:
            raise ValueError("offset must be >= 0")
        self.limit = int(self.limit)
        self.offset = int(self.offset)
        if self.min_citations is not None and int(self.min_citations) < 0:
            raise ValueError("min_citations must be >= 0")
        if self.min_citations is not None:
            self.min_citations = int(self.min_citations)
        if self.recent_days is not None and not 1 <= int(self.recent_days) <= 365:
            raise ValueError("recent_days must be between 1 and 365")
        if self.recent_days is not None:
            self.recent_days = int(self.recent_days)
        self.publication_types = _clean_list(self.publication_types)
        self.fields_of_study = _clean_list(self.fields_of_study)
        self.sources = normalize_sources(self.sources)
        self._normalize_dates()
        self._validate_years()

    def _normalize_dates(self) -> None:
        if self.date_from:
            date.fromisoformat(self.date_from)
        if self.date_to:
            date.fromisoformat(self.date_to)
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be <= date_to")
        if not self.date_from and self.year_from:
            self.date_from = f"{int(self.year_from):04d}-01-01"
        if not self.date_to and self.year_to:
            self.date_to = f"{int(self.year_to):04d}-12-31"

    def _validate_years(self) -> None:
        current = date.today().year + 1
        for name, value in (("year_from", self.year_from), ("year_to", self.year_to)):
            if value is not None and not 1000 <= int(value) <= current:
                raise ValueError(f"{name} must be between 1000 and {current}")
        if self.year_from and self.year_to and int(self.year_from) > int(self.year_to):
            raise ValueError("year_from must be <= year_to")

    @property
    def advanced(self) -> bool:
        if self.query_mode == "advanced":
            return True
        if self.query_mode == "plain":
            return False
        return bool(_ADVANCED_RE.search(self.query))

    @property
    def effective_query(self) -> str:
        parts = [self.query] if self.query else []
        if self.author:
            parts.append(f'author:{quote_value(self.author)}')
        if self.venue:
            parts.append(f'journal:{quote_value(self.venue)}')
        return " AND ".join(parts)

    @property
    def identifier(self) -> tuple[str | None, str | None]:
        doi = extract_doi(self.query)
        if doi:
            return "doi", doi
        arxiv = extract_arxiv_id(self.query)
        if arxiv:
            return "arxiv", arxiv
        return None, None


@dataclass(slots=True)
class SourceReport:
    source: str
    results: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    endpoint: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0
    authenticated: bool = False

    def provenance(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "endpoint": self.endpoint,
            "parameters": redact_params(self.params),
            "total": self.total,
            "retrieved": len(self.results),
            "warnings": self.warnings,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "authenticated": self.authenticated,
        }


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(",")
    return [str(value).strip() for value in values if str(value).strip()]


def normalize_sources(sources: Any) -> list[str] | None:
    values = _clean_list(sources)
    if not values:
        return None
    normalized: list[str] = []
    for raw in values:
        name = _SOURCE_ALIASES.get(raw.lower(), raw.lower().replace("-", "_"))
        if name == "auto":
            continue
        if name == "all":
            for source in SUPPORTED_SOURCES:
                if source not in normalized:
                    normalized.append(source)
            continue
        if name not in SUPPORTED_SOURCES:
            raise ValueError(
                f"unsupported source {raw!r}; choose from {', '.join(SUPPORTED_SOURCES)}"
            )
        if name not in normalized:
            normalized.append(name)
    return normalized or None


def quote_value(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value
    if re.search(r"\s", value):
        return '"' + value.replace('"', r'\"') + '"'
    return value


def extract_doi(value: str) -> str | None:
    match = _DOI_RE.match((value or "").strip().rstrip(".,;"))
    return match.group(1) if match else None


def extract_arxiv_id(value: str) -> str | None:
    return normalize_arxiv_id(value or "")


def valid_contact_email(value: str | None) -> bool:
    if not value or not _EMAIL_RE.match(value):
        return False
    lowered = value.lower()
    return not lowered.endswith(("@example.com", "@example.invalid", "@invalid"))


def extract_fields(query: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for match in _FIELD_RE.finditer(query or ""):
        value = match.group("value").strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace(r'\"', '"')
        fields.setdefault(match.group("field").lower(), []).append(value)
    return fields


_FIELD_MAPS: dict[str, dict[str, str | None]] = {
    "pubmed": {
        "title": "TI", "author": "AU", "abstract": "TIAB", "journal": "TA",
        "doi": "AID", "category": "SB", "mesh": "MH", "fulltext": "TW",
    },
    "pmc": {
        "title": "TI", "author": "AU", "abstract": "TIAB", "journal": "TA",
        "doi": "AID", "category": "SB", "mesh": "MH", "fulltext": "TW",
    },
    "arxiv": {
        "title": "ti", "author": "au", "abstract": "abs", "journal": "jr",
        "doi": "all", "category": "cat", "mesh": None, "fulltext": "all",
    },
    "core": {
        "title": "title", "author": "authors", "abstract": "abstract",
        "journal": "dataProviders", "doi": "doi", "category": "documentType",
        "mesh": None, "fulltext": "fullText",
    },
}


def adapt_query(query: str, source: str, *, exact: bool = False) -> tuple[str, list[str]]:
    """Translate the common field syntax while preserving boolean structure."""
    warnings: list[str] = []
    query = (query or "").strip()
    mapping = _FIELD_MAPS.get(source)

    def replace_field(match: re.Match[str]) -> str:
        field_name = match.group("field").lower()
        value = match.group("value")
        if mapping is None:
            warnings.append(
                f"{source} has no exact {field_name}: mapping; searched the value as text"
            )
            return value
        target = mapping.get(field_name)
        if not target:
            warnings.append(
                f"{source} does not support {field_name}: exactly; searched the value as text"
            )
            return value
        if source in {"pubmed", "pmc"}:
            return f"({value}[{target}])"
        return f"{target}:{value}"

    translated = _FIELD_RE.sub(replace_field, query)
    if exact and translated and not _ADVANCED_RE.search(query) and not (
        translated.startswith('"') and translated.endswith('"')
    ):
        translated = '"' + translated.replace('"', r'\"') + '"'
    if source == "arxiv":
        translated = re.sub(r"(?i)\bNOT\b", "ANDNOT", translated)
    elif source == "semantic_scholar":
        translated = re.sub(r"(?i)\s+AND\s+", " + ", translated)
        translated = re.sub(r"(?i)\s+OR\s+", " | ", translated)
        translated = re.sub(r"(?i)\bNOT\s+", "-", translated)
    return translated.strip(), list(dict.fromkeys(warnings))


def redact_params(params: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in params.items():
        lowered = key.lower()
        if any(token in lowered for token in ("api_key", "apikey", "token", "secret")):
            redacted[key] = "***" if value else ""
        elif lowered in {"email", "mailto"}:
            redacted[key] = "configured" if value else ""
        else:
            redacted[key] = value
    return redacted


def deduplicate_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in results:
        item = dict(raw)
        doi = (item.get("doi") or "").lower().strip()
        arxiv_id = re.sub(r"(?i)v\d+$", "", item.get("arxiv_id") or "")
        pmid = str(item.get("pmid") or "")
        pmcid = str(item.get("pmcid") or "")
        title_key = re.sub(r"\W+", "", (item.get("title") or "").lower())[:160]
        year = str(item.get("year") or "")
        key = (
            f"doi:{doi}" if doi else
            f"arxiv:{arxiv_id}" if arxiv_id else
            f"pmid:{pmid}" if pmid else
            f"pmcid:{pmcid}" if pmcid else
            f"title:{title_key}:{year}"
        )
        if not (doi or arxiv_id or pmid or pmcid or title_key):
            continue
        source = item.get("source") or "unknown"
        item.setdefault("sources", [source])
        item.setdefault(
            "identifier",
            item.get("doi") or item.get("arxiv_id") or item.get("pmcid")
            or item.get("pmid") or item.get("core_id") or "",
        )
        if key not in merged:
            merged[key] = item
            continue
        current = merged[key]
        for field_name, value in item.items():
            if field_name in {"source", "sources"}:
                continue
            if value not in (None, "", [], {}) and current.get(field_name) in (
                None, "", [], {},
            ):
                current[field_name] = value
        current["is_oa"] = bool(current.get("is_oa") or item.get("is_oa"))
        if item.get("cited_by_count", 0) > current.get("cited_by_count", 0):
            current["cited_by_count"] = item["cited_by_count"]
        for name in item.get("sources", [source]):
            if name not in current["sources"]:
                current["sources"].append(name)
        current["source"] = "+".join(current["sources"])
    return list(merged.values())


def sort_results(results: list[dict[str, Any]], sort: str) -> None:
    if sort == "cited_by_count":
        results.sort(key=lambda item: item.get("cited_by_count", 0) or 0, reverse=True)
    elif sort in {"publication_date", "updated_date"}:
        field_name = "updated_date" if sort == "updated_date" else "publication_date"
        results.sort(
            key=lambda item: item.get(field_name) or str(item.get("year") or ""),
            reverse=True,
        )
