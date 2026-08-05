"""MCP server exposing scansci-pdf tools for Claude Code."""

import logging
import sys

from mcp.server.fastmcp import FastMCP

from .config import load_config, save_config
from .institutional.config_adapter import ConfigAdapter
from .institutional.fetcher import PaperFetcher

# Logging must go to stderr (stdout is used by MCP stdio transport)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

mcp = FastMCP("scansci-pdf")

# Lazy-initialized shared fetcher instance
_fetcher: PaperFetcher | None = None


def _get_fetcher() -> PaperFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = PaperFetcher(ConfigAdapter.load())
    return _fetcher


@mcp.tool()
async def fetch_paper(identifier: str, format: str = "markdown") -> str:
    """Fetch an academic paper's full text by DOI or URL.

    Uses a 7-step cascade: cache → OA (Unpaywall + arXiv) → Elsevier API →
    DOI resolve → CARSI → publisher PDF → browser PDF → campus gateway.
    Results are cached locally.

    Args:
        identifier: DOI (e.g. "10.1038/nphys1509") or article URL.
        format: Output format - "markdown" (default), "json", or "text".
    """
    fetcher = _get_fetcher()
    result = fetcher.fetch_with_result(identifier)

    if format == "json":
        return result.to_json()
    elif format == "text":
        return result.to_text()
    else:
        return result.to_markdown(include_pdf_path=True)


@mcp.tool()
async def search_papers(query: str, limit: int = 10, year_range: str = "") -> str:
    """Search for academic papers via OpenAlex, Semantic Scholar, and Crossref.

    Returns a list of papers with titles, authors, DOIs, and citation counts.
    Use the DOIs from results with fetch_paper to get full text.

    Args:
        query: Search query (e.g. "organic photovoltaics silver nanowire").
        limit: Maximum number of results (1-100, default 10).
        year_range: Optional year filter (e.g. "2020-2024" or "2020-").
    """
    from .search import search_papers as _search

    year_from = None
    year_to = None
    if year_range:
        parts = year_range.split("-")
        if len(parts) == 2:
            try:
                year_from = int(parts[0]) if parts[0] else None
                year_to = int(parts[1]) if parts[1] else None
            except ValueError:
                pass

    results = _search(query, limit=limit, year_from=year_from, year_to=year_to)

    if not results:
        return "No results found."

    lines = [f"Found {len(results)} results:\n"]
    for i, r in enumerate(results, 1):
        authors = r.get("authors", [])
        authors_str = ", ".join(authors[:3])
        if len(authors) > 3:
            authors_str += " et al."

        lines.append(f"### {i}. {r.get('title', 'Untitled')}")
        lines.append(f"- **Authors:** {authors_str}")
        if r.get("year"):
            lines.append(f"- **Year:** {r['year']}")
        if r.get("doi"):
            lines.append(f"- **DOI:** {r['doi']}")
        lines.append(f"- **Citations:** {r.get('cited_by_count', 0)}")
        if r.get("is_oa"):
            lines.append(f"- **Open Access:** yes")
        if r.get("abstract"):
            lines.append(f"- **Abstract:** {r['abstract'][:200]}...")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def get_paper_metadata(doi: str) -> str:
    """Get metadata for a paper by DOI via Unpaywall.

    Returns title, authors, year, abstract, OA status, and PDF URL.
    Lighter than fetch_paper - does not download full text.

    Args:
        doi: The DOI of the paper (e.g. "10.1038/nphys1509").
    """
    from .institutional.sources.unpaywall import check_oa

    config = ConfigAdapter.load()
    result = check_oa(doi, email=config.email)

    lines = [f"# {result.title or 'Untitled'}"]
    if result.authors:
        lines.append(f"**Authors:** {', '.join(result.authors)}")
    if result.year:
        lines.append(f"**Year:** {result.year}")
    if result.journal:
        lines.append(f"**Journal:** {result.journal}")
    lines.append(f"**DOI:** {doi}")
    lines.append(f"**Open Access:** {'yes' if result.is_oa else 'no'}")
    if result.source:
        lines.append(f"**OA source:** {result.source}")
    if result.pdf_url:
        lines.append(f"**PDF URL:** {result.pdf_url}")
    if result.html_url:
        lines.append(f"**Landing page:** {result.html_url}")

    return "\n".join(lines)


@mcp.tool()
async def configure_school(school: str) -> str:
    """Configure the institutional access school.

    Sets the school name for WebVPN/EZproxy authentication.
    Run 'scansci-pdf schools' to see available schools.

    Args:
        school: School name (e.g. "清华大学", "Nanjing University").
    """
    from .schools import search_schools

    matches = search_schools(school)
    if not matches:
        return f"No school matching '{school}' found. Run 'scansci-pdf schools' to list available schools."

    config = load_config()
    config["vpnsci_school"] = matches[0].name
    save_config(config)

    # Reset cached fetcher so it picks up new config
    global _fetcher
    _fetcher = None

    if len(matches) == 1:
        return f"Configured school: {matches[0].name} ({matches[0].host})"
    else:
        names = ", ".join(m.name for m in matches[:5])
        return f"Best match: {matches[0].name}. Other matches: {names}"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
