"""FastAPI web interface for ScanSci PDF."""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .advanced_search import search_papers_detailed
from .config import load_config
from .identifiers import is_arxiv_identifier, normalize_doi
from .log import get_logger
from .sources import download

log = get_logger()

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.cache_size = 0
_TEMPLATE_REQUEST_FIRST = (
    next(iter(inspect.signature(templates.TemplateResponse).parameters), "")
    == "request"
)

app = FastAPI(title="ScanSci PDF", description="Academic paper downloader web UI")


# --- Request/Response models ---

class DownloadRequest(BaseModel):
    identifier: str


class SearchRequest(BaseModel):
    query: str = ""
    limit: int = Field(default=50, ge=1, le=100)
    detailed: bool = False
    sources: list[str] | None = None
    query_mode: str = "auto"
    exact: bool = False
    offset: int = Field(default=0, ge=0)
    year_from: int | None = None
    year_to: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    sort: str = "relevance"
    author: str | None = None
    author_id: str | None = None
    publication_types: list[str] | None = None
    fields_of_study: list[str] | None = None
    venue: str | None = None
    category: str | None = None
    open_access_only: bool = False
    has_abstract: bool | None = None
    min_citations: int | None = Field(default=None, ge=0)
    language: str | None = None
    recent_days: int | None = Field(default=None, ge=1, le=365)
    enrich_open_access: bool = False


# --- Helper ---

_DOI_PATTERN = re.compile(r"^10\.\d{4,}/")
_DOI_URL_PATTERN = re.compile(r"https?://doi\.org/")


def _is_doi_or_arxiv(text: str) -> bool:
    """Check if input looks like a DOI or arXiv ID (not a title)."""
    text = text.strip()
    if is_arxiv_identifier(text):
        return True
    if _DOI_URL_PATTERN.match(text):
        return True
    if _DOI_PATTERN.match(text):
        return True
    return False


def _check_sources(config: dict[str, Any]) -> dict[str, Any]:
    """Check availability of key download sources."""
    sources: dict[str, bool | str] = {}

    # CloakBrowser
    try:
        from .browser_engine import is_available
        cb_available = is_available(config)
        sources["cloakbrowser"] = cb_available
    except Exception:
        sources["cloakbrowser"] = False

    # Tor
    try:
        from .tor import check_tor_circuit
        tor_ok = check_tor_circuit(config)
        sources["tor"] = tor_ok
    except Exception:
        sources["tor"] = False

    # WebVPN
    sources["webvpn"] = bool(config.get("webvpn_cookies"))

    # CARSI
    sources["carsi"] = bool(config.get("carsi_cookies"))

    # Sci-Hub
    sources["scihub"] = config.get("scihub_enabled", True)

    return sources


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if _TEMPLATE_REQUEST_FIRST:
        return templates.TemplateResponse(request, "index.html")
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/download")
async def api_download(req: DownloadRequest):
    """Download a paper by DOI or arXiv ID. Returns PDF file or error JSON."""
    import asyncio

    identifier = req.identifier.strip()
    if not identifier:
        return JSONResponse({"success": False, "error": "Empty identifier"}, status_code=400)

    # Normalize DOI URL to bare DOI
    if _DOI_URL_PATTERN.match(identifier):
        identifier = _DOI_URL_PATTERN.sub("", identifier)

    # If input looks like a title (not DOI/arXiv), try to resolve first
    if not _is_doi_or_arxiv(identifier):
        from .resolver import resolve_title_to_doi
        config = load_config()
        doi = resolve_title_to_doi(identifier, config)
        if doi:
            identifier = doi
        else:
            return JSONResponse(
                {"success": False, "error": f"Could not resolve title to DOI: {identifier}"},
                status_code=404,
            )

    # Run download in a worker thread to avoid blocking the event loop.
    # asyncio.to_thread is the modern replacement for run_in_executor(None, fn)
    # and avoids deprecation warnings around get_event_loop() in async context.
    result = await asyncio.to_thread(download, identifier)

    if result.get("success"):
        file_path = result.get("file", "")
        source = result.get("source", "unknown")
        if file_path and Path(file_path).exists():
            filename = Path(file_path).name
            return FileResponse(
                file_path,
                media_type="application/pdf",
                filename=filename,
                headers={"X-ScanSci-Source": source},
            )
        return JSONResponse(
            {"success": False, "error": "PDF file not found on disk after download"},
            status_code=500,
        )

    # Enhance error response with actionable guidance
    error_response = dict(result)
    config = load_config()
    sources = _check_sources(config)
    error_response["sources"] = sources

    # Add specific guidance based on what's available
    guidance = error_response.get("guidance", [])
    if not sources.get("cloakbrowser"):
        guidance.insert(0, "CloakBrowser is not running. Start it to enable browser-based downloads for paywalled papers.")
    if not sources.get("tor"):
        guidance.append("Tor is not running. Start Tor for anonymous Sci-Hub access.")

    error_response["guidance"] = guidance
    return JSONResponse(error_response, status_code=404)


@app.post("/api/search")
async def api_search(req: SearchRequest):
    """Search papers; detailed mode adds retrieval provenance."""
    query = req.query.strip()
    if not query and not (
        req.author or req.author_id or req.date_from or req.date_to or req.recent_days
    ):
        return JSONResponse([], status_code=400)

    # Normalize DOI URL
    if _DOI_URL_PATTERN.match(query):
        query = _DOI_URL_PATTERN.sub("", query)

    # Preserve the legacy direct-identifier response unless detailed mode is requested.
    if not req.detailed and _is_doi_or_arxiv(query):
        return JSONResponse([{"doi": normalize_doi(query) if not is_arxiv_identifier(query) else query, "title": "", "is_direct": True}])

    try:
        result = await asyncio.to_thread(
            search_papers_detailed,
            query=query, limit=req.limit, sources=req.sources,
            query_mode=req.query_mode, exact=req.exact, offset=req.offset,
            year_from=req.year_from, year_to=req.year_to,
            date_from=req.date_from, date_to=req.date_to, sort=req.sort,
            author=req.author, author_id=req.author_id,
            publication_types=req.publication_types,
            fields_of_study=req.fields_of_study, venue=req.venue,
            category=req.category, open_access_only=req.open_access_only,
            has_abstract=req.has_abstract, min_citations=req.min_citations,
            language=req.language, recent_days=req.recent_days,
            enrich_open_access=req.enrich_open_access,
        )
    except ValueError as exc:
        if req.detailed:
            return JSONResponse({"results": [], "error": str(exc)}, status_code=400)
        return JSONResponse([], status_code=400)
    return JSONResponse(result if req.detailed else result["results"])


@app.get("/api/status")
async def api_status():
    """Health check with source availability."""
    config = load_config()
    sources = _check_sources(config)

    return JSONResponse({
        "status": "ok",
        "output_dir": config.get("output_dir", ""),
        "sources": sources,
    })
