"""Paper search via OpenAlex, Semantic Scholar, Crossref, and PubMed."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import load_config


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
    from .network import _get_session, request_timeout
    config = load_config()
    try:
        session = _get_session(config)
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
            timeout=request_timeout(config),
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    results = []
    for work in data.get("results", []):
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


def search_papers(
    query: str,
    limit: int = 50,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    """Search papers from OpenAlex + Semantic Scholar + Crossref + PubMed in parallel."""
    all_results: list[dict[str, Any]] = []
    per_source = max(10, limit)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_search_openalex, query, per_source, year_from, year_to, sort): "openalex",
            pool.submit(_search_semantic_scholar, query, per_source, year_from, year_to): "semantic_scholar",
            pool.submit(_search_crossref, query, per_source, year_from, year_to): "crossref",
            pool.submit(_search_pubmed, query, per_source, year_from, year_to): "pubmed",
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
            if not existing.get("pmid") and r.get("pmid"):
                existing["pmid"] = r["pmid"]
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
    from .network import _get_session, request_timeout
    config = load_config()
    try:
        session = _get_session(config)
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
                timeout=request_timeout(config),
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
    from .network import _get_session, request_timeout
    config = load_config()
    try:
        session = _get_session(config)
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
            timeout=request_timeout(config),
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


def _search_pubmed(
    query: str, limit: int = 10,
    year_from: int | None = None, year_to: int | None = None,
) -> list[dict[str, Any]]:
    """Search PubMed via NCBI E-utilities API."""
    from .network import _get_session, request_timeout
    config = load_config()

    try:
        session = _get_session(config)

        # Step 1: Search for PMIDs
        params: dict[str, Any] = {
            "db": "pubmed",
            "term": query,
            "retmax": min(limit, 200),
            "retmode": "json",
            "sort": "relevance",
        }
        if year_from or year_to:
            y_from = year_from or "1900"
            y_to = year_to or "2026"
            params["mindate"] = str(y_from)
            params["maxdate"] = str(y_to)
            params["datetype"] = "pdat"

        resp = session.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=params,
            timeout=request_timeout(config),
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        pmids = data.get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return []

        # Step 2: Fetch summaries for PMIDs
        time.sleep(0.3)  # respect NCBI rate limit
        summary_resp = session.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
            timeout=request_timeout(config),
        )
        if summary_resp.status_code != 200:
            return []
        summary_data = summary_resp.json()
    except Exception:
        return []

    results = []
    for pmid in pmids:
        info = summary_data.get("result", {}).get(pmid, {})
        if not info or isinstance(info, str):
            continue

        # Extract DOI from articleids
        doi = ""
        for aid in info.get("articleids", []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value", "")
                break

        if not doi:
            continue

        authors = []
        for a in info.get("authors", [])[:5]:
            name = a.get("name", "")
            if name:
                authors.append(name)

        # Year from pubdate
        pubdate = info.get("pubdate", "")
        year = pubdate[:4] if pubdate and pubdate[:4].isdigit() else ""

        title = info.get("title", "")
        # Clean up title (remove trailing period)
        if title.endswith("."):
            title = title[:-1]

        results.append({
            "title": title,
            "doi": doi,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "authors": authors,
            "year": year,
            "cited_by_count": 0,  # PubMed doesn't provide citation count
            "abstract": "",  # Summary doesn't include abstract
            "is_oa": False,
            "oa_url": "",
            "pmid": pmid,
            "source": "pubmed",
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
