"""Paper search via OpenAlex, Semantic Scholar, and Crossref."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from .config import load_config

_SEARCH_TIMEOUT = 30  # seconds, longer than default because these are public APIs
_USER_AGENT = "scansci-pdf/1.5 (https://github.com/Rimagination/scansci-pdf)"


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


def _lookup_author_id(name: str) -> tuple[str | None, str, int, int]:
    """Resolve an author name to an OpenAlex author ID.

    Returns (author_id, display_name, works_count, cited_by_count).
    author_id is None if no match found.

    Matching strategy:
    1. Exact case-insensitive name match
    2. If none, pick by highest works_count
    """
    try:
        session = _plain_session()
        resp = session.get(
            "https://api.openalex.org/authors",
            params={"search": name, "per_page": 10},
            timeout=_SEARCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return None, "", 0, 0
        data = resp.json()
    except Exception:
        return None, "", 0, 0

    candidates = data.get("results", [])
    if not candidates:
        return None, "", 0, 0

    name_lower = name.lower().strip()
    # Generate name variants (handle Chinese/Western name order)
    name_parts = name_lower.split()
    name_variants = {name_lower}
    if len(name_parts) == 2:
        # "Fang Jingyun" ↔ "Jingyun Fang"
        name_variants.add(f"{name_parts[1]} {name_parts[0]}")
    elif len(name_parts) >= 3:
        # Try last-name-first ordering for 3+ word names
        name_variants.add(f"{name_parts[-1]} {' '.join(name_parts[:-1])}")

    # First: try exact case-insensitive name match (including variants)
    exact_matches = [
        a for a in candidates
        if (a.get("display_name") or "").lower().strip() in name_variants
    ]
    if exact_matches:
        best = max(
            exact_matches,
            key=lambda a: (a.get("cited_by_count", 0), a.get("works_count", 0)),
        )
    else:
        # Fallback: highest cited_by_count (prefer authoritative consolidated profiles)
        best = max(
            candidates,
            key=lambda a: (a.get("cited_by_count", 0), a.get("works_count", 0)),
        )

    author_id = best.get("id", "").split("/")[-1] if best.get("id") else ""
    display = best.get("display_name", name)
    works = best.get("works_count", 0)
    cited = best.get("cited_by_count", 0)
    return (author_id if author_id else None, display, works, cited)


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
        matched_name = None
        matched_works = 0
        matched_cited = 0
        if author and not author_id:
            resolved, matched_name, matched_works, matched_cited = _lookup_author_id(author)
            if not resolved:
                return []
            author_id = resolved

        if author_id:
            results = _search_openalex_by_author(
                author_id, limit=limit,
                year_from=year_from, year_to=year_to, sort=sort,
            )
            # Attach author match metadata to first result
            if results and (matched_name or author):
                results[0]["_author_match"] = {
                    "name": matched_name or author or "",
                    "id": author_id,
                    "works_count": matched_works,
                    "cited_by_count": matched_cited,
                }
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
