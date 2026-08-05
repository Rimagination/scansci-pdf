"""Reusable publisher access catalog and live verification helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from .publisher_pdf_router import build_pdf_candidates, discover_pdf_candidates_from_html
from .publisher_profiles import get_publisher_profile, list_publisher_profiles

CATALOG_PATH = Path(__file__).parent.parent / "data" / "publisher_access_catalog.json"
BROWSER_VERIFICATION_MATRIX_PATH = Path(__file__).parent.parent / "data" / "publisher_browser_verification_matrix.json"
INSTITUTIONAL_IDENTITY_POLICY_PATH = Path(__file__).parent.parent / "data" / "institutional_identity_policy.json"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
}


def load_publisher_access_catalog(path: str | Path | None = None) -> dict[str, Any]:
    """Load the machine-readable publisher access catalog."""
    catalog_path = Path(path) if path else CATALOG_PATH
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def load_publisher_browser_verification_matrix(path: str | Path | None = None) -> dict[str, Any]:
    """Load browser-backed publisher PDF verification verdicts."""
    matrix_path = Path(path) if path else BROWSER_VERIFICATION_MATRIX_PATH
    return json.loads(matrix_path.read_text(encoding="utf-8"))


def load_institutional_identity_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load reusable policy for choosing institutional identity routes."""
    policy_path = Path(path) if path else INSTITUTIONAL_IDENTITY_POLICY_PATH
    return json.loads(policy_path.read_text(encoding="utf-8"))


def verify_publisher_access(
    publisher: str,
    *,
    session: requests.Session | None = None,
    probe_pdf: bool = True,
    max_candidates: int = 4,
    timeout: int = 20,
) -> dict[str, Any]:
    """Resolve a sample DOI and classify official PDF candidate behavior.

    The verifier does not save PDFs and does not bypass login, CAPTCHA, or
    publisher challenges. It records whether routes are reachable, blocked by
    identity/challenge, or missing.
    """
    catalog = load_publisher_access_catalog()
    profile = get_publisher_profile(publisher)
    key = _profile_key(publisher)
    entry = catalog["publishers"][key]
    doi = entry["verification"]["sample_doi"]
    http = session or requests.Session()

    landing = _get(
        http,
        f"https://doi.org/{doi}",
        timeout=timeout,
        headers=DEFAULT_HEADERS,
        allow_redirects=True,
    )
    landing_url = getattr(landing, "url", "")
    history_urls = [getattr(item, "url", "") for item in getattr(landing, "history", [])]
    chain = [url for url in [*history_urls, landing_url] if url]
    landing_status = int(getattr(landing, "status_code", 0) or 0)
    landing_text = _response_text(landing)

    discovered = discover_pdf_candidates_from_html(landing_text, landing_url) if landing_text else []
    candidates = build_pdf_candidates(
        profile,
        doi,
        source_url=landing_url,
        discovered_urls=discovered,
    )

    candidate_probes: list[dict[str, Any]] = []
    if probe_pdf:
        for url in candidates[:max(0, max_candidates)]:
            candidate_probes.append(_probe_candidate(http, url, timeout=timeout))

    expected_domains = [domain.lower() for domain in entry["verification"].get("expected_domains", [])]
    chain_haystack = " ".join(chain).lower()
    resolved_to_expected_domain = any(domain in chain_haystack for domain in expected_domains)
    observed_access = _observed_access(landing, candidate_probes)
    return {
        "profile_key": key,
        "display_name": entry["display_name"],
        "sample_doi": doi,
        "landing_status": landing_status,
        "landing_url": landing_url,
        "redirect_chain": chain,
        "resolved_to_expected_domain": resolved_to_expected_domain,
        "pdf_candidates": candidates,
        "candidate_probes": candidate_probes,
        "observed_access": observed_access,
        "identity": entry["identity"],
        "persistence": entry["persistence"],
        "link_characteristics": entry["link_characteristics"],
    }


def verify_publishers(
    publishers: list[str] | None = None,
    *,
    probe_pdf: bool = True,
    max_candidates: int = 4,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    keys = publishers or list_publisher_profiles()
    session = requests.Session()
    return [
        verify_publisher_access(
            key,
            session=session,
            probe_pdf=probe_pdf,
            max_candidates=max_candidates,
            timeout=timeout,
        )
        for key in keys
    ]


def _profile_key(publisher: str) -> str:
    profile = get_publisher_profile(publisher)
    for key in list_publisher_profiles():
        if get_publisher_profile(key) is profile:
            return key
    raise ValueError(f"Unknown publisher profile: {publisher}")


def _get(session: requests.Session, url: str, **kwargs):
    return session.get(url, **kwargs)


def _probe_candidate(session: requests.Session, url: str, *, timeout: int) -> dict[str, Any]:
    headers = {
        **DEFAULT_HEADERS,
        "Accept": "application/pdf,text/html;q=0.8,*/*;q=0.5",
        "Range": "bytes=0-4095",
    }
    try:
        response = _get(
            session,
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        result = {
            "url": url,
            "final_url": getattr(response, "url", url),
            "status": int(getattr(response, "status_code", 0) or 0),
            "content_type": _header(response, "content-type"),
            "classification": _classify_response(response),
        }
        close = getattr(response, "close", None)
        if close:
            close()
        return result
    except requests.RequestException as exc:
        return {
            "url": url,
            "final_url": "",
            "status": 0,
            "content_type": "",
            "classification": "network_error",
            "error": str(exc),
        }


def _classify_response(response) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    content_type = _header(response, "content-type").lower()
    final_url = str(getattr(response, "url", "")).lower()
    text = _response_text(response, limit=4000).lower()
    login_markers = (
        "login",
        "signin",
        "sign in",
        "openathens",
        "institution",
        "shibboleth",
        "access through",
    )
    challenge_markers = ("captcha", "robot", "challenge", "perfdrive", "cloudflare", "validate.perfdrive")
    if 200 <= status < 300 and "pdf" in content_type:
        return "pdf_accessible"
    if status in {401, 402, 403}:
        if any(marker in final_url or marker in text for marker in challenge_markers):
            return "challenge_or_bot_check"
        return "identity_required"
    if status == 404:
        return "not_found"
    if status >= 500:
        return "server_error"
    if any(marker in final_url or marker in text for marker in challenge_markers):
        return "challenge_or_bot_check"
    if any(marker in final_url or marker in text for marker in login_markers):
        return "identity_required"
    if 200 <= status < 300 and "html" in content_type:
        return "html_or_reader"
    if 300 <= status < 400:
        return "redirect"
    return "unknown"


def _observed_access(landing, candidate_probes: list[dict[str, Any]]) -> str:
    classifications = [probe["classification"] for probe in candidate_probes]
    if "pdf_accessible" in classifications:
        return "pdf_accessible"
    if "identity_required" in classifications:
        return "identity_required"
    if "challenge_or_bot_check" in classifications or _classify_response(landing) == "challenge_or_bot_check":
        return "challenge_or_bot_check"
    if not candidate_probes:
        return _classify_response(landing)
    if all(item == "not_found" for item in classifications):
        return "not_found"
    return classifications[0] if classifications else "unknown"


def _response_text(response, *, limit: int = 200_000) -> str:
    content_type = _header(response, "content-type").lower()
    if content_type and "html" not in content_type and "text" not in content_type and "xml" not in content_type:
        return ""
    try:
        return str(getattr(response, "text", "") or "")[:limit]
    except Exception:
        return ""


def _header(response, name: str) -> str:
    headers = getattr(response, "headers", {}) or {}
    try:
        return str(headers.get(name) or headers.get(name.title()) or "")
    except AttributeError:
        return ""
