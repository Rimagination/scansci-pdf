"""OpenAlex OA direct link source + Content API."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

import requests

from ..network import fetch_json, polite_delay, USER_AGENT, request_timeout, proxy_dict, select_proxy_for_url
from ..pdf_utils import download_pdf, is_plausible_pdf_url, is_pdf_file, success, _response_looks_pdf


def try_openalex_oa(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try OpenAlex best_oa_location for OA PDF link."""
    q = urllib.parse.quote(doi, safe="")
    email = config.get("email", "")
    mailto = f"?mailto={email}" if email else ""
    url = f"https://api.openalex.org/works/doi:{q}{mailto}"
    try:
        payload = fetch_json(url, config)
        if not payload:
            return None
        oa = payload.get("open_access", {})
        if not oa.get("is_oa"):
            return None
        pdf_url = oa.get("oa_url") or ""
        if not pdf_url:
            primary = payload.get("primary_location", {})
            if isinstance(primary, dict):
                pdf_url = primary.get("pdf_url") or ""
        if pdf_url and is_plausible_pdf_url(pdf_url):
            polite_delay(config)
            result = download_pdf(pdf_url, output_path, config, "OpenAlexOA")
            if result:
                result["doi"] = doi
                result["identifier"] = doi
                return result
    except Exception:
        pass
    return None


def try_openalex_content_api(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try OpenAlex Content API for direct PDF download.

    Uses content.openalex.org which returns a 302 redirect to a signed
    Cloudflare R2 URL containing the actual PDF.
    Requires a free API key from https://openalex.org/users (set openalex_api_key in config).
    """
    api_key = config.get("openalex_api_key", "")
    if not api_key:
        return None

    q = urllib.parse.quote(doi, safe="")
    meta_url = f"https://api.openalex.org/works/doi:{q}?mailto={api_key}"
    try:
        payload = fetch_json(meta_url, config)
        if not payload:
            return None

        work_id = payload.get("id", "")
        if not work_id:
            return None
        if "/" in work_id:
            work_id = work_id.rsplit("/", 1)[-1]

        oa = payload.get("open_access", {})
        if not oa.get("is_oa"):
            return None

        content_url = f"https://content.openalex.org/works/{work_id}.pdf?mailto={api_key}"
        polite_delay(config)

        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": USER_AGENT})

        resp = s.get(content_url, timeout=request_timeout(config),
                     proxies=proxy_dict(select_proxy_for_url(content_url, config)),
                     allow_redirects=True, stream=True)

        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not _response_looks_pdf(resp, first):
            return None

        from .publishers import _write_pdf_atomic
        if not _write_pdf_atomic(output_path, first, iterator):
            return None

        if is_pdf_file(output_path):
            return success(doi, output_path, "OpenAlexContent")
    except Exception:
        pass
    return None
