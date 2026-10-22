"""Paper search via OpenAlex, Semantic Scholar, and Crossref."""

from __future__ import annotations

import logging
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import requests

from .config import load_config

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 30  # seconds, longer than default because these are public APIs
_USER_AGENT = "scansci-pdf/1.5 (https://github.com/Rimagination/scansci-pdf)"
_AUTHOR_CANDIDATES = 10  # OpenAlex author profiles fetched per name lookup


def _plain_session() -> requests.Session:
    """A requests session without proxy, for public academic APIs."""
    s = requests.Session()
    s.headers.update({"User-Agent": _USER_AGENT, "mailto": "scansci-pdf@example.invalid"})
    return s


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        if isinstance(positions, list):
            for pos in positions:
                if isinstance(pos, int):
                    word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)[:500]


def _search_openalex(
    query: str, limit: int = 10,
    year_from: int | None = None, year_to: int | None = None,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    try:
        session = _plain_session()
        params: dict[str, Any] = {"search": query, "per_page": limit}
        filters = []
        if year_from or year_to:
            y_from = year_from or 1900
            y_to = year_to or 2026
            filters.append(f"publication_year:{y_from}-{y_to}")
        if filters:
            params["filter"] = ",".join(filters)
        if sort:
            sort_key = sort if ":" in sort else f"{sort}:desc"
            params["sort"] = sort_key
        resp = session.get(
            "https://api.openalex.org/works",
            params=params,
            timeout=_SEARCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    return _parse_openalex_works(data.get("results", []))


def _parse_openalex_works(works: list[dict]) -> list[dict[str, Any]]:
    """Parse OpenAlex works into unified result format."""
    results = []
    for work in works:
        doi_raw = work.get("doi", "") or ""
        doi = doi_raw.replace("https://doi.org/", "") if doi_raw else ""
        if not doi:
            continue
        authors = [
            a.get("author", {}).get("display_name", "")
            for a in (work.get("authorships") or [])[:5]
        ]
        oa = work.get("open_access") or {}
        best_oa = work.get("best_oa_location") or {}
        is_oa = oa.get("is_oa", False)
        oa_url = best_oa.get("pdf_url") or best_oa.get("landing_page_url") or oa.get("oa_url") or ""
        results.append({
            "title": work.get("title", ""),
            "doi": doi,
            "url": work.get("id", ""),
            "authors": authors,
            "year": work.get("publication_year", ""),
            "cited_by_count": work.get("cited_by_count", 0),
            "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
            "is_oa": is_oa,
            "oa_url": oa_url,
            "source": "openalex",
        })
    return results


@dataclass(frozen=True)
class AuthorMatch:
    """A resolved OpenAlex author profile, plus the alternatives it beat."""

    id: str
    display_name: str
    works_count: int
    cited_by_count: int
    exact: bool = True
    alternatives: tuple[dict[str, Any], ...] = ()


_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
_NAME_SPLIT = re.compile(r"[\s,]+")


def _name_tokens(name: str) -> list[str]:
    """Split a display name into comparable tokens: lowercase, dots and suffixes dropped."""
    tokens = _NAME_SPLIT.split((name or "").strip().lower())
    return [
        t.replace(".", "")
        for t in tokens
        if t and t.replace(".", "") not in _NAME_SUFFIXES
    ]


def _tokens_compatible(query: list[str], candidate: list[str]) -> bool:
    """Every query token appears in `candidate`; leftover candidate tokens are initials."""
    remaining = list(candidate)
    for token in query:
        if token in remaining:
            remaining.remove(token)
        else:
            return False
    return all(len(token) == 1 for token in remaining)


def _name_compatible(query: str, candidate: str) -> bool:
    """True when two name strings plausibly denote the same person.

    Tolerates middle names/initials ("Shilong Piao" ≈ "Shilong L. Piao") and
    reversed order ("Fang Jingyun" ≈ "Jingyun Fang"). A single-letter query
    token never expands to a spelled-out candidate token, so "J. Smith" does
    not silently resolve to "John Smith".
    """
    q, c = _name_tokens(query), _name_tokens(candidate)
    if not q or not c:
        return False
    return _tokens_compatible(q, c) or _tokens_compatible(c, q)


def _author_alternative(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": (candidate.get("id") or "").split("/")[-1],
        "display_name": candidate.get("display_name", ""),
        "works_count": candidate.get("works_count", 0),
        "cited_by_count": candidate.get("cited_by_count", 0),
    }


def _resolve_author(name: str) -> AuthorMatch | None:
    """Resolve an author name to the most likely OpenAlex author profile.

    OpenAlex keeps thin duplicate profiles beside authoritative ones, and a
    query without the middle initial is an *exact* match for the thin profile
    while the real one carries the initial: "Shilong Piao" matches the 1-work
    duplicate exactly, and the 999-work "Shilong L. Piao" only approximately.
    Ranking inside exact matches alone therefore selects the duplicate, so
    every name-compatible profile competes and the largest one wins.
    """
    try:
        session = _plain_session()
        resp = session.get(
            "https://api.openalex.org/authors",
            params={"search": name, "per_page": _AUTHOR_CANDIDATES},
            timeout=_SEARCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    candidates = [c for c in (data.get("results") or []) if c.get("id")]
    if not candidates:
        return None

    compatible = [
        c for c in candidates
        if _name_compatible(name, c.get("display_name") or "")
    ]
    ranked = sorted(
        compatible or candidates,
        key=lambda a: (a.get("cited_by_count", 0), a.get("works_count", 0)),
        reverse=True,
    )
    best = ranked[0]
    display = best.get("display_name") or name

    return AuthorMatch(
        id=(best.get("id") or "").split("/")[-1],
        display_name=display,
        works_count=best.get("works_count", 0),
        cited_by_count=best.get("cited_by_count", 0),
        exact=_name_tokens(display) == _name_tokens(name),
        alternatives=tuple(_author_alternative(a) for a in ranked[1:4]),
    )


def _lookup_author_id(name: str) -> tuple[str | None, str, int, int]:
    """Backwards-compatible 4-tuple wrapper around :func:`_resolve_author`."""
    match = _resolve_author(name)
    if not match:
        return None, "", 0, 0
    return match.id, match.display_name, match.works_count, match.cited_by_count


def _search_openalex_by_author(
    author_id: str,
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    """Search OpenAlex works by author ID."""
    try:
        session = _plain_session()
        filters = [f"authorships.author.id:{author_id}"]
        if year_from or year_to:
            y_from = year_from or 1900
            y_to = year_to or 2026
            filters.append(f"publication_year:{y_from}-{y_to}")

        params: dict[str, Any] = {
            "filter": ",".join(filters),
            "per_page": limit,
        }
        if sort:
            sort_key = sort if ":" in sort else f"{sort}:desc"
            params["sort"] = sort_key

        resp = session.get(
            "https://api.openalex.org/works",
            params=params,
            timeout=_SEARCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    return _parse_openalex_works(data.get("results", []))


def search_papers_v2(
    query: str = "",
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str | None = None,
    *,
    author: str | None = None,
    author_id: str | None = None,
) -> list[dict[str, Any]]:
    """Keyword search via ScanSci Find's 13-source discovery when available.

    Falls back transparently to the built-in three-source ``search_papers``
    when the scansci-find CLI is missing or the discovery call fails. Author
    search always stays on the built-in OpenAlex path (ScanSci Find exposes
    no author-specific endpoint through its CLI).

    Result fields are unchanged (doi/title/authors/year/cited_by_count/is_oa/
    oa_url/source) so existing callers are unaffected.
    """
    if author_id or author:
        return search_papers(
            query, limit=limit, year_from=year_from, year_to=year_to, sort=sort,
            author=author, author_id=author_id,
        )
    if not query:
        return []

    try:
        from .discovery import find_cli_available, search as discovery_search, to_legacy_results
        if not find_cli_available():
            return search_papers(query, limit=limit, year_from=year_from, year_to=year_to, sort=sort)
        with tempfile.TemporaryDirectory(prefix="scansci_find_") as tmp:
            payload = discovery_search(
                query, tmp, depth="quick", limit=max(limit * 3, 30),
                year_from=year_from, year_to=year_to,
            )
            results = to_legacy_results(payload.get("candidates") or [], limit=limit * 3)
        logger.info("search: discovery engine returned %d results", len(results))
    except Exception as exc:  # noqa: BLE001 - degrade gracefully to 3-source search
        logger.warning("search: discovery engine failed (%s), falling back to 3-source", exc)
        return search_papers(query, limit=limit, year_from=year_from, year_to=year_to, sort=sort)

    if sort == "cited_by_count":
        results.sort(key=lambda x: x.get("cited_by_count", 0), reverse=True)
    elif sort == "publication_date":
        results.sort(key=lambda x: x.get("year", 0), reverse=True)
    return results[:limit]


def search_papers(
    query: str = "",
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str | None = None,
    *,
    author: str | None = None,
    author_id: str | None = None,
) -> list[dict[str, Any]]:
    """Search papers from OpenAlex + Semantic Scholar + Crossref in parallel.

    When `author` or `author_id` is provided, searches by author instead of keyword.
    - `author`: resolves author name → OpenAlex author ID → works
    - `author_id`: directly searches works by OpenAlex author ID
    """
    # --- Author-based search (fast path) ---
    if author_id or author:
        match: AuthorMatch | None = None
        if author and not author_id:
            match = _resolve_author(author)
            if not match:
                return []
            author_id = match.id

        if author_id:
            results = _search_openalex_by_author(
                author_id, limit=limit,
                year_from=year_from, year_to=year_to, sort=sort,
            )
            # Attach author match metadata to first result. `exact` and
            # `alternatives` let callers spot a disambiguation they may want to
            # override with an explicit author_id.
            if results and (match or author):
                meta: dict[str, Any] = {
                    "name": match.display_name if match else (author or ""),
                    "id": author_id,
                    "works_count": match.works_count if match else 0,
                    "cited_by_count": match.cited_by_count if match else 0,
                }
                if match:
                    meta["query"] = author
                    meta["exact"] = match.exact
                    if match.alternatives:
                        meta["alternatives"] = list(match.alternatives)
                results[0]["_author_match"] = meta
            return results

    # --- Keyword-based search (existing parallel path) ---
    if not query:
        return []

    all_results: list[dict[str, Any]] = []
    per_source = max(5, limit)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_search_openalex, query, per_source, year_from, year_to, sort): "openalex",
            pool.submit(_search_semantic_scholar, query, per_source, year_from, year_to): "semantic_scholar",
            pool.submit(_search_crossref, query, per_source, year_from, year_to): "crossref",
        }
        for future in as_completed(futures, timeout=30):
            try:
                all_results.extend(future.result())
            except Exception:
                pass

    # Deduplicate by DOI (prefer entry with more info)
    seen: dict[str, dict[str, Any]] = {}
    for r in all_results:
        doi = r.get("doi", "").lower()
        if not doi:
            continue
        if doi not in seen:
            seen[doi] = r
        else:
            existing = seen[doi]
            # Merge: keep fields from whichever entry has more data
            if not existing.get("abstract") and r.get("abstract"):
                existing["abstract"] = r["abstract"]
            if not existing.get("is_oa") and r.get("is_oa"):
                existing["is_oa"] = True
                existing["oa_url"] = r.get("oa_url", "")
            if r.get("cited_by_count", 0) > existing.get("cited_by_count", 0):
                existing["cited_by_count"] = r["cited_by_count"]
            existing["source"] = existing.get("source", "") + "+" + r.get("source", "")

    # Sort by relevance or citations
    merged = list(seen.values())
    if sort == "cited_by_count":
        merged.sort(key=lambda x: x.get("cited_by_count", 0), reverse=True)
    elif sort == "publication_date":
        merged.sort(key=lambda x: x.get("year", 0), reverse=True)

    return merged[:limit]


def _search_semantic_scholar(
    query: str, limit: int = 10,
    year_from: int | None = None, year_to: int | None = None,
) -> list[dict[str, Any]]:
    import time
    try:
        session = _plain_session()
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "fields": "title,externalIds,authors,year,citationCount,abstract,isOpenAccess,openAccessPdf",
        }
        year_filter = []
        if year_from:
            year_filter.append(str(year_from))
        if year_to:
            year_filter.append(str(year_to))
        if year_filter:
            params["year"] = "-".join(year_filter) if len(year_filter) == 2 else year_filter[0]

        for attempt in range(3):
            resp = session.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params=params,
                timeout=_SEARCH_TIMEOUT,
            )
            if resp.status_code == 200:
                break
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            return []
        else:
            return []
        data = resp.json()
    except Exception:
        return []

    results = []
    for paper in data.get("data", []):
        ext_ids = paper.get("externalIds") or {}
        doi = (ext_ids.get("DOI") or "").strip()
        if not doi:
            continue
        authors = [a.get("name", "") for a in (paper.get("authors") or [])[:5]]
        oa_info = paper.get("openAccessPdf") or {}
        results.append({
            "title": paper.get("title", ""),
            "doi": doi,
            "url": f"https://api.semanticscholar.org/DOI:{doi}",
            "authors": authors,
            "year": paper.get("year", ""),
            "cited_by_count": paper.get("citationCount", 0),
            "abstract": (paper.get("abstract") or "")[:500],
            "is_oa": paper.get("isOpenAccess", False),
            "oa_url": oa_info.get("url", ""),
        })
    return results


def _search_crossref(
    query: str, limit: int = 10,
    year_from: int | None = None, year_to: int | None = None,
) -> list[dict[str, Any]]:
    try:
        session = _plain_session()
        params: dict[str, Any] = {
            "query": query,
            "rows": limit,
            "select": "DOI,title,author,published-print,is-referenced-by-count,abstract,link,container-title",
        }
        filters = []
        if year_from:
            filters.append(f"from-pub-date:{year_from}")
        if year_to:
            filters.append(f"until-pub-date:{year_to}")
        if filters:
            params["filter"] = ",".join(filters)

        resp = session.get(
            "https://api.crossref.org/works",
            params=params,
            timeout=_SEARCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    results = []
    for item in data.get("message", {}).get("items", []):
        doi = item.get("DOI", "")
        if not doi:
            continue
        titles = item.get("title", [])
        title = titles[0] if titles else ""
        authors = []
        for a in (item.get("author") or [])[:5]:
            name = " ".join(filter(None, [a.get("given"), a.get("family")]))
            if name:
                authors.append(name)
        # Year from published-print
        pub_date = item.get("published-print", {}).get("date-parts", [[]])
        year = pub_date[0][0] if pub_date and pub_date[0] else ""
        # OA links
        links = item.get("link", [])
        oa_url = ""
        for link in links:
            if link.get("content-type") == "application/pdf":
                oa_url = link.get("URL", "")
                break
        # Abstract (may contain HTML tags)
        abstract = (item.get("abstract") or "")[:500]
        if abstract:
            import re
            abstract = re.sub(r"<[^>]+>", "", abstract)
        results.append({
            "title": title,
            "doi": doi,
            "url": f"https://doi.org/{doi}",
            "authors": authors,
            "year": year,
            "cited_by_count": item.get("is-referenced-by-count", 0),
            "abstract": abstract,
            "is_oa": bool(oa_url),
            "oa_url": oa_url,
        })
    return results


def search_by_title(title: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Search OpenAlex by title and return best match with DOI."""
    from difflib import SequenceMatcher
    from .network import _get_session, request_timeout

    if not title or len(title) < 10:
        return None

    if config is None:
        config = load_config()

    try:
        session = _get_session(config)
        resp = session.get(
            "https://api.openalex.org/works",
            params={"search": title, "per_page": 5},
            timeout=request_timeout(config),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    title_lower = title.lower().strip()
    best = None
    best_score = 0.0

    for work in data.get("results", []):
        result_title = (work.get("title") or "").lower().strip()
        if not result_title:
            continue
        score = SequenceMatcher(None, title_lower, result_title).ratio()
        if score > best_score:
            best_score = score
            doi_raw = work.get("doi", "") or ""
            doi = doi_raw.replace("https://doi.org/", "") if doi_raw else ""
            authors = [
                a.get("author", {}).get("display_name", "")
                for a in (work.get("authorships") or [])[:5]
            ]
            best = {
                "title": work.get("title", ""),
                "doi": doi,
                "authors": authors,
                "year": work.get("publication_year", ""),
                "score": round(score, 3),
            }

    if best_score >= 0.75 and best and best.get("doi"):
        return best
    return None
