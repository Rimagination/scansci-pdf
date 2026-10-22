"""Paper search via OpenAlex, Semantic Scholar, Crossref, and NCPSSD (CJK)."""

from __future__ import annotations

import logging
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from .config import load_config

logger = logging.getLogger(__name__)

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


def _sciverse_ready() -> bool:
    """True when a Sciverse API key is configured (cheap, no network)."""
    try:
        from .sources.sciverse import is_configured
        return is_configured(load_config())
    except Exception:
        return False


def _search_sciverse(
    query: str,
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[dict[str, Any]]:
    """Sciverse agentic-search source; never raises into the search race."""
    from .sources.sciverse import SciverseError, is_configured, search_sciverse
    config = load_config()
    if not is_configured(config):
        return []
    try:
        return search_sciverse(
            query,
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            config=config,
            # Discovery/download scenario: spend one extra meta call to make
            # evidence hits DOI-ready. Disabling the meta lane disables this.
            enrich_dois=bool(config.get("sciverse_meta_search_enabled", True)),
        )
    except SciverseError as exc:
        logger.warning("search: sciverse failed (%s: %s)", exc.code or exc.status_code, exc)
    except Exception as exc:  # noqa: BLE001 - one source must not break search
        logger.warning("search: sciverse failed (%s)", exc)
    return []


def _sciverse_meta_ready() -> bool:
    """True when meta-search is enabled and a Sciverse key is configured."""
    try:
        from .sources.sciverse import is_configured
        config = load_config()
        return bool(config.get("sciverse_meta_search_enabled", True)) and is_configured(config)
    except Exception:
        return False


def _search_sciverse_meta(
    query: str,
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[dict[str, Any]]:
    """Sciverse meta-search lane; never raises into the search race."""
    from .sources.sciverse import SciverseError, is_configured, meta_search_papers
    config = load_config()
    if not config.get("sciverse_meta_search_enabled", True) or not is_configured(config):
        return []
    try:
        return meta_search_papers(
            query,
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            config=config,
        )
    except SciverseError as exc:
        logger.warning("search: sciverse meta-search failed (%s: %s)", exc.code or exc.status_code, exc)
    except Exception as exc:  # noqa: BLE001 - one source must not break search
        logger.warning("search: sciverse meta-search failed (%s)", exc)
    return []


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

    When ``sciverse_api_key`` is configured, Sciverse agentic-search is merged
    in as an additional semantic source (merged records carry ``doc_id`` and
    citation-safe ``evidence`` chunks).

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

    extra_tasks: list[tuple[Any, tuple[Any, ...]]] = []
    if _sciverse_ready():
        extra_tasks.append((_search_sciverse, (query, max(limit, 10), year_from, year_to)))
    if _sciverse_meta_ready():
        extra_tasks.append((_search_sciverse_meta, (query, max(limit, 10), year_from, year_to)))
    if _query_has_cjk(query):
        extra_tasks.append((_search_ncpssd, (query, max(limit, 10), year_from, year_to)))
    if extra_tasks:
        pool = ThreadPoolExecutor(max_workers=len(extra_tasks))
        futures = [pool.submit(fn, *args) for fn, args in extra_tasks]
        try:
            for future in as_completed(futures, timeout=30):
                try:
                    results = _merge_search_results(results, future.result())
                except Exception:
                    pass
        except TimeoutError:
            logger.warning("search: optional extra sources timed out; using partial results")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    if sort == "cited_by_count":
        results.sort(key=lambda x: x.get("cited_by_count", 0), reverse=True)
    elif sort == "publication_date":
        results.sort(key=lambda x: x.get("year", 0), reverse=True)
    return results[:limit]


def _query_has_cjk(query: str) -> bool:
    try:
        from .chinese import has_cjk
        return has_cjk(query)
    except Exception:
        return False


def _search_ncpssd(
    query: str, limit: int = 10,
    year_from: int | None = None, year_to: int | None = None,
) -> list[dict[str, Any]]:
    """Search NCPSSD's Chinese humanities/social-science archive."""
    from .chinese import normalize_cn_text, normalize_doi_loose
    from .sources.ncpssd import ncpssd_record_url, search_ncpssd

    config = load_config()
    if not config.get("ncpssd_enabled", True):
        return []
    rows = search_ncpssd(query, config, page_size=max(limit, 10))
    results: list[dict[str, Any]] = []
    for row in rows:
        year = str(row.get("years") or "")
        try:
            year_value: Any = int(year) if year else ""
        except ValueError:
            year_value = ""
        if year_from and isinstance(year_value, int) and year_value < year_from:
            continue
        if year_to and isinstance(year_value, int) and year_value > year_to:
            continue
        doi = normalize_doi_loose(str(row.get("doi") or ""))
        authors = [
            name for name in (
                normalize_cn_text(part.split("[", 1)[0])
                for part in re.split(r"[;；]", str(row.get("creator") or ""))
            ) if name
        ]
        results.append({
            "title": normalize_cn_text(str(row.get("title") or "")),
            "doi": doi,
            "url": ncpssd_record_url(str(row.get("id") or "")) if row.get("id") else (
                f"https://doi.org/{doi}" if doi else ""
            ),
            "authors": authors[:5],
            "year": year_value,
            "cited_by_count": 0,
            "abstract": normalize_cn_text(str(row.get("remark") or ""))[:500],
            "is_oa": bool(row.get("pdfurl")),
            "oa_url": str(row.get("pdfurl") or ""),
            "source": "ncpssd",
            "ncpssd_id": str(row.get("id") or ""),
            "journal": normalize_cn_text(str(row.get("cbw_name") or "")),
        })
    return results


def _merge_search_results(
    base: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge result lists across doc_id, DOI and title, preserving provenance.

    ``doc_id`` is Sciverse's full-text content hash: when the agentic and
    meta lanes return the same paper under different titles, this joins them
    so the DOI-less evidence hit inherits the meta hit's DOI/`unique_id`.
    DOI-bearing and DOI-less duplicates are then unified by DOI/title.
    """
    try:
        from .chinese import normalize_title_for_match
    except Exception:
        def normalize_title_for_match(value: str) -> str:
            return (value or "").lower()

    records: list[dict[str, Any]] = []
    by_doc_id: dict[str, dict[str, Any]] = {}
    by_doi: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}

    for item in [*base, *extra]:
        doc_id = str(item.get("doc_id") or "").strip()
        doi = str(item.get("doi") or "").strip().lower()
        title_key = normalize_title_for_match(str(item.get("title") or ""))
        existing = by_doc_id.get(doc_id) if doc_id else None
        if existing is None:
            existing = by_doi.get(doi) if doi else None
        if existing is None and title_key:
            candidate = by_title.get(title_key)
            # Never collapse two records that carry different DOIs merely
            # because their titles match.
            if candidate is not None and (not doi or not candidate.get("doi")):
                existing = candidate

        if existing is None:
            records.append(item)
            existing = item
        else:
            if not existing.get("doi") and item.get("doi"):
                existing["doi"] = item["doi"]
            if not existing.get("url") and item.get("url"):
                existing["url"] = item["url"]
            if not existing.get("abstract") and item.get("abstract"):
                existing["abstract"] = item["abstract"]
            if not existing.get("is_oa") and item.get("is_oa"):
                existing["is_oa"] = True
                existing["oa_url"] = item.get("oa_url", "")
            if item.get("cited_by_count", 0) > existing.get("cited_by_count", 0):
                existing["cited_by_count"] = item["cited_by_count"]
            for field in (
                "doc_id", "unique_id", "snippet", "primary_topic",
                "primary_topic_domain", "journal", "venue_type", "score",
                "language", "fwci", "influential_citation_count", "year",
                "ncpssd_id", "arxiv_id",
            ):
                if not existing.get(field) and item.get(field):
                    existing[field] = item[field]
            if item.get("evidence"):
                evidence = existing.setdefault("evidence", [])
                if isinstance(evidence, list):
                    seen_chunks = {
                        e.get("chunk_id") for e in evidence if isinstance(e, dict)
                    }
                    for chunk in item["evidence"]:
                        if isinstance(chunk, dict) and chunk.get("chunk_id") not in seen_chunks:
                            evidence.append(chunk)
                            seen_chunks.add(chunk.get("chunk_id"))

            sources = [
                source for source in str(existing.get("source") or "").split("+") if source
            ]
            new_source = str(item.get("source") or "")
            if new_source and new_source not in sources:
                sources.append(new_source)
                existing["source"] = "+".join(sources)

        if doc_id:
            by_doc_id[doc_id] = existing
        if doi:
            by_doi[doi] = existing
        if title_key:
            by_title[title_key] = existing

    return records


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

    Chinese queries additionally hit NCPSSD (国家哲学社会科学文献中心), which
    indexes humanities/social-science journals that the Western indexes miss.

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

    has_cjk = _query_has_cjk(query)
    sciverse_on = _sciverse_ready()
    sciverse_meta_on = _sciverse_meta_ready()
    worker_count = 3 + (1 if has_cjk else 0) + (1 if sciverse_on else 0) + (1 if sciverse_meta_on else 0)
    pool = ThreadPoolExecutor(max_workers=worker_count)
    futures = {
        pool.submit(_search_openalex, query, per_source, year_from, year_to, sort): "openalex",
        pool.submit(_search_semantic_scholar, query, per_source, year_from, year_to): "semantic_scholar",
        pool.submit(_search_crossref, query, per_source, year_from, year_to): "crossref",
    }
    if has_cjk:
        futures[pool.submit(_search_ncpssd, query, per_source, year_from, year_to)] = "ncpssd"
    if sciverse_on:
        futures[pool.submit(_search_sciverse, query, per_source, year_from, year_to)] = "sciverse"
    if sciverse_meta_on:
        futures[pool.submit(_search_sciverse_meta, query, per_source, year_from, year_to)] = "sciverse-meta"
    try:
        for future in as_completed(futures, timeout=30):
            try:
                all_results.extend(future.result())
            except Exception:
                pass
    except TimeoutError:
        logger.warning("search: some sources timed out; using partial results")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Deduplicate by DOI, falling back to normalized title for DOI-less rows
    # (Chinese NCPSSD records and Sciverse hits can lack a DOI).
    merged = _merge_search_results(all_results, [])
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
