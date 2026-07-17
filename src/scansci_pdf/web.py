"""FastAPI web interface for ScanSci PDF."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config import load_config
from .identifiers import is_arxiv_identifier, normalize_doi
from .log import get_logger
from .search import search_papers
from .sources import download

log = get_logger()

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.cache_size = 0

app = FastAPI(title="ScanSci PDF", description="Academic paper downloader web UI")

# Active download tasks for SSE tracking
_active_downloads: dict[str, dict[str, Any]] = {}
# Detached tasks must be strongly referenced until their worker thread exits.
# asyncio's loop only keeps weak task references; the done callback consumes
# exceptions and removes the task once cooperative cleanup has completed.
_background_download_tasks: set[asyncio.Task[Any]] = set()

# One-time download tokens: token -> {"path": str, "expires": float, "filename": str}
_download_tokens: dict[str, dict[str, Any]] = {}
_TOKEN_TTL = 300  # 5 minutes
_MAX_DOWNLOAD_TOKENS = 256


def _consume_background_download(task: asyncio.Task[Any]) -> None:
    try:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
    finally:
        _background_download_tasks.discard(task)


def _track_background_download(task: asyncio.Task[Any]) -> None:
    """Keep an un-awaited download task alive and always consume its result."""
    if task.done():
        _consume_background_download(task)
        return
    _background_download_tasks.add(task)
    task.add_done_callback(_consume_background_download)


def _cleanup_download_tokens() -> None:
    now = time.time()
    expired = [token for token, info in _download_tokens.items() if now > info["expires"]]
    for token in expired:
        _download_tokens.pop(token, None)
    while len(_download_tokens) >= _MAX_DOWNLOAD_TOKENS:
        oldest = min(_download_tokens, key=lambda token: _download_tokens[token]["expires"])
        _download_tokens.pop(oldest, None)


# --- Request/Response models ---

class DownloadRequest(BaseModel):
    identifier: str


class SearchRequest(BaseModel):
    query: str
    limit: int = 10


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


def _create_download_token(file_path: str, filename: str) -> str:
    """Create a one-time download token for a file. Returns the token string."""
    _cleanup_download_tokens()
    token = uuid.uuid4().hex
    _download_tokens[token] = {
        "path": file_path,
        "filename": filename,
        "expires": time.time() + _TOKEN_TTL,
    }
    return token


def _consume_download_token(token: str) -> dict[str, Any] | None:
    """Consume a one-time download token. Returns file info or None if invalid/expired."""
    info = _download_tokens.pop(token, None)
    if not info:
        return None
    if time.time() > info["expires"]:
        return None
    fp = Path(info["path"])
    if not fp.exists():
        return None
    return {"path": str(fp), "filename": info["filename"]}


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
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/download")
async def api_download(req: DownloadRequest, request: Request):
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
        doi = await asyncio.to_thread(resolve_title_to_doi, identifier, config)
        if doi:
            identifier = doi
        else:
            return JSONResponse(
                {"success": False, "error": f"Could not resolve title to DOI: {identifier}"},
                status_code=404,
            )

    # Keep the worker observable and cooperatively cancel it if the client leaves.
    cancel_event = threading.Event()
    download_task = asyncio.create_task(
        asyncio.to_thread(download, identifier, _cancel_event=cancel_event)
    )
    client_disconnected = False
    result: dict[str, Any] | None = None
    task_result_consumed = False
    try:
        while not download_task.done():
            if await request.is_disconnected():
                client_disconnected = True
                cancel_event.set()
                break
            await asyncio.wait({download_task}, timeout=0.1)
        if not client_disconnected:
            try:
                result = await download_task
            finally:
                task_result_consumed = True
    finally:
        cancel_event.set()
        if not task_result_consumed:
            _track_background_download(download_task)

    if client_disconnected:
        return JSONResponse(
            {"success": False, "error": "Client disconnected", "cancelled": True},
            status_code=499,
        )

    if result is None:
        return JSONResponse(
            {"success": False, "error": "Download worker returned no result"},
            status_code=500,
        )

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
    sources = await asyncio.to_thread(_check_sources, config)
    error_response["sources"] = sources

    # Add specific guidance based on what's available
    guidance = error_response.get("guidance", [])
    if not sources.get("cloakbrowser"):
        guidance.insert(0, "CloakBrowser is not running. Start it to enable browser-based downloads for paywalled papers.")
    if not sources.get("tor"):
        guidance.append("Tor is not running. Start Tor for anonymous Sci-Hub access.")

    error_response["guidance"] = guidance
    return JSONResponse(error_response, status_code=404)


@app.get("/api/download/file/{token}")
async def api_download_file(token: str):
    """Download a file using a one-time token. Token is consumed on use."""
    info = _consume_download_token(token)
    if not info:
        return JSONResponse({"success": False, "error": "Invalid or expired download token"}, status_code=404)
    return FileResponse(
        info["path"],
        media_type="application/pdf",
        filename=info["filename"],
    )


@app.post("/api/download/stream")
async def api_download_stream(req: DownloadRequest, request: Request):
    """Download a paper with real-time SSE status updates.

    Returns a stream of JSON events:
    - {"type": "start", "identifier": "...", "task_id": "..."}
    - {"type": "progress", "phase": "...", "source": "...", "message": "..."}
    - {"type": "success", "file": "...", "source": "...", "task_id": "..."}
    - {"type": "error", "error": "...", "task_id": "..."}
    """
    identifier = req.identifier.strip()
    if not identifier:
        return JSONResponse({"success": False, "error": "Empty identifier"}, status_code=400)

    # Normalize DOI URL to bare DOI
    if _DOI_URL_PATTERN.match(identifier):
        identifier = _DOI_URL_PATTERN.sub("", identifier)

    # If input looks like a title, resolve to DOI first
    if not _is_doi_or_arxiv(identifier):
        from .resolver import resolve_title_to_doi
        config = load_config()
        doi = await asyncio.to_thread(resolve_title_to_doi, identifier, config)
        if doi:
            identifier = doi
        else:
            return JSONResponse(
                {"success": False, "error": f"Could not resolve title to DOI: {identifier}"},
                status_code=404,
            )

    task_id = str(uuid.uuid4())[:8]

    async def event_generator():
        event_queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        loop = asyncio.get_running_loop()
        callback_open = threading.Event()
        callback_open.set()
        download_cancel_event = threading.Event()
        download_task: asyncio.Task | None = None
        task_result_consumed = False

        def progress_callback(event_type: str, **kwargs):
            if not callback_open.is_set() or event_type != "progress" or loop.is_closed():
                return
            event = {"type": "progress", "task_id": task_id, **kwargs}

            def enqueue() -> None:
                if callback_open.is_set() and not event_queue.full():
                    event_queue.put_nowait(event)

            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(enqueue)

        _active_downloads[task_id] = {
            "identifier": identifier,
            "status": "running",
            "started_at": loop.time(),
        }

        try:
            yield f"data: {json.dumps({'type': 'start', 'identifier': identifier, 'task_id': task_id})}\n\n"
            download_task = asyncio.create_task(
                asyncio.to_thread(
                    download,
                    identifier,
                    _progress_callback=progress_callback,
                    _cancel_event=download_cancel_event,
                )
            )

            while not download_task.done():
                if await request.is_disconnected():
                    download_cancel_event.set()
                    _active_downloads[task_id]["status"] = "cancelling"
                    return
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

            try:
                result = await download_task
            finally:
                task_result_consumed = True
            while not event_queue.empty():
                event = event_queue.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"

            if result and result.get("success"):
                file_path = result.get("file", "")
                source = result.get("source", "unknown")
                fp = Path(file_path)
                if fp.exists():
                    token = _create_download_token(file_path, fp.name)
                    log.info(f"SSE [{task_id}] success: {fp.name} ({fp.stat().st_size} bytes), token issued")
                    yield f"data: {json.dumps({'type': 'success', 'filename': fp.name, 'source': source, 'task_id': task_id, 'download_token': token})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'File not found after download', 'task_id': task_id})}\n\n"
            else:
                error = (result or {}).get("error") or (result or {}).get("reason") or "Download failed"
                yield f"data: {json.dumps({'type': 'error', 'error': error, 'task_id': task_id})}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not await request.is_disconnected():
                yield f"data: {json.dumps({'type': 'error', 'error': str(exc), 'task_id': task_id})}\n\n"
        finally:
            callback_open.clear()
            download_cancel_event.set()
            _active_downloads.pop(task_id, None)
            if download_task is not None and not task_result_consumed:
                _track_background_download(download_task)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/search")
async def api_search(req: SearchRequest):
    """Search papers by keyword. Returns list of results."""
    query = req.query.strip()
    if not query:
        return JSONResponse([], status_code=400)

    # Normalize DOI URL
    if _DOI_URL_PATTERN.match(query):
        query = _DOI_URL_PATTERN.sub("", query)

    # If input is a DOI/arXiv, skip search and return a single-item result
    if _is_doi_or_arxiv(query):
        return JSONResponse([{"doi": normalize_doi(query) if not is_arxiv_identifier(query) else query, "title": "", "is_direct": True}])

    results = await asyncio.to_thread(search_papers, query, limit=req.limit)
    return JSONResponse(results)


@app.get("/api/status")
async def api_status():
    """Health check with source availability."""
    config = load_config()
    sources = await asyncio.to_thread(_check_sources, config)

    return JSONResponse({
        "status": "ok",
        "output_dir": config.get("output_dir", ""),
        "sources": sources,
        "active_downloads": len(_active_downloads),
    })
@app.get("/api/downloads/active")
async def api_active_downloads():
    """List currently active downloads."""
    return JSONResponse({
        "active": [
            {
                "task_id": tid,
                "identifier": info["identifier"],
                "status": info["status"],
                "elapsed": asyncio.get_event_loop().time() - info["started_at"],
            }
            for tid, info in _active_downloads.items()
        ]
    })
