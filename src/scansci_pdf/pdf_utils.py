"""PDF detection, validation, and download helpers."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from .network import fetch

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


def is_pdf_file(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < 1000:
            return False
        with path.open("rb") as fh:
            header = fh.read(5)
            if header != b"%PDF-":
                return False
            fh.seek(max(0, size - 1024))
            tail = fh.read()
            return b"%%EOF" in tail
    except OSError:
        return False


def is_suspicious_pdf(path: Path, min_size_kb: int = 50) -> bool:
    """Check if a PDF is suspiciously small (likely a preview/cover page)."""
    try:
        size_kb = path.stat().st_size / 1024
        return size_kb < min_size_kb
    except OSError:
        return False


def suspicious_pdf(identifier: str, file_path: Path, source: str) -> dict[str, Any]:
    """Return a failure result for a suspicious (too small) PDF."""
    size_kb = round(file_path.stat().st_size / 1024, 1)
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "success": False,
        "identifier": identifier,
        "doi": identifier,
        "source": source,
        "reason": f"Suspicious PDF ({size_kb} KB) — likely a preview or cover page, not full text",
    }


def is_plausible_pdf_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    host = (parsed.hostname or "").lower()
    combined = (path + "?" + query).lower()

    reject_markers = ["/data-providers/", "/data-provider/", "/providers/", "/journals/", "/subjects/"]
    if any(marker in combined for marker in reject_markers):
        return False

    if path.endswith(".pdf"):
        return True
    if "/pdf" in path or "download/pdf" in path:
        return True
    if "format=pdf" in query or "type=pdf" in query:
        return True
    if ("hal.science" in host or "archives-ouvertes" in host) and path.endswith("/document"):
        return True
    return False


def _response_looks_pdf(resp: requests.Response, first_chunk: bytes) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    return first_chunk.startswith(b"%PDF-") or "application/pdf" in ctype


def success(identifier: str, file_path: Path, source: str) -> dict[str, Any]:
    size_kb = round(file_path.stat().st_size / 1024, 1)
    return {
        "success": True,
        "identifier": identifier,
        "doi": identifier,
        "file": str(file_path),
        "size_kb": size_kb,
        "source": source,
    }


def fail(
    identifier: str,
    reason: str = "not found",
    extra: dict[str, Any] | None = None,
    *,
    error_type: str = "",
    action: str = "",
) -> dict[str, Any]:
    result = {"success": False, "identifier": identifier, "doi": identifier, "reason": reason}
    if error_type:
        result["error_type"] = error_type
    if action:
        result["action"] = action
    if extra:
        result.update(extra)
    return result


def dedupe(items: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item:
            continue
        item = item.strip() if isinstance(item, str) else str(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def iter_urls(obj: Any) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and ("url" in key.lower() or value.startswith("http")):
                yield value
            else:
                yield from iter_urls(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_urls(item)


def extract_pdf_url_from_html(html: str, base_url: str) -> str | None:
    urls: list[str] = []
    for match in re.finditer(
        r"""<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)["']""", html, re.I
    ):
        urls.append(urllib.parse.urljoin(base_url, match.group(1)))
    for match in re.finditer(
        r"""<meta[^>]+content=["']([^"']+)["'][^>]+name=["']citation_pdf_url["']""", html, re.I
    ):
        urls.append(urllib.parse.urljoin(base_url, match.group(1)))
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["iframe", "embed", "a"]):
            candidate = tag.get("src") or tag.get("href")
            if candidate:
                urls.append(urllib.parse.urljoin(base_url, candidate))
    else:
        for match in re.finditer(r"""(?:src|href)=["']([^"']+)["']""", html, re.I):
            urls.append(urllib.parse.urljoin(base_url, match.group(1)))

    for url in dedupe(urls):
        if is_plausible_pdf_url(url):
            return url
    return None


def download_pdf(
    url: str,
    output_path: Path,
    config: dict[str, Any],
    source: str,
    *,
    require_pdf_like_url: bool = True,
    use_tor: bool = False,
    cookies: Any = None,
) -> dict[str, Any] | None:
    if require_pdf_like_url and not is_plausible_pdf_url(url):
        return None

    try:
        if cookies is not None:
            from .network import request_timeout, proxy_dict, select_proxy_for_url, USER_AGENT
            session = requests.Session()
            session.trust_env = False
            session.headers.update({"User-Agent": USER_AGENT})
            session.cookies.update(cookies)
            resp = session.get(
                url,
                timeout=request_timeout(config),
                proxies=proxy_dict(select_proxy_for_url(url, config)),
                allow_redirects=True,
                stream=True,
            )
        else:
            resp = fetch(url, config, stream=True, use_tor=use_tor)
        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first_chunk = next(iterator, b"")
        if not _response_looks_pdf(resp, first_chunk):
            return None

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".part")
        try:
            with tmp_path.open("wb") as fh:
                fh.write(first_chunk)
                for chunk in iterator:
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(output_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        if is_pdf_file(output_path):
            return success(output_path.stem, output_path, source)
        else:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        return None
    return None
