"""Unpaywall source for open-access papers."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG
from ..network import fetch, polite_delay
from ..pdf_utils import download_pdf, is_plausible_pdf_url, dedupe, fail


def _is_placeholder_email(email: str) -> bool:
    """Unpaywall 422s placeholder/anonymous addresses; they are never usable."""
    return (
        not email
        or "@example.invalid" in email
        or "@example.com" in email
        or email.startswith("mailto:")
    )


def extract_unpaywall_pdf_candidates(payload: dict[str, Any]) -> list[str]:
    """Extract PDF URLs prioritized: best_oa > repository > publisher.

    Repository URLs (EuropePMC, arXiv, PMC) are more likely to be freely
    accessible than publisher URLs which often require subscription.
    """
    repo_urls: list[str] = []
    publisher_urls: list[str] = []

    # best_oa_location first
    best = payload.get("best_oa_location")
    if isinstance(best, dict):
        url = best.get("url_for_pdf") or ""
        if is_plausible_pdf_url(url):
            if best.get("host_type") == "publisher":
                publisher_urls.append(url)
            else:
                repo_urls.append(url)

    # Separate by host_type
    all_locations = payload.get("oa_locations", [])
    if isinstance(all_locations, list):
        for loc in all_locations:
            if not isinstance(loc, dict):
                continue
            url = loc.get("url_for_pdf") or ""
            if not is_plausible_pdf_url(url):
                continue
            is_publisher = loc.get("host_type") == "publisher"
            if is_publisher:
                if url not in publisher_urls:
                    publisher_urls.append(url)
            else:
                if url not in repo_urls:
                    repo_urls.append(url)

    # Repository URLs first (more likely free), then publisher URLs
    return dedupe(repo_urls + publisher_urls)


def try_unpaywall(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    raw_email = str(config.get("email") or DEFAULT_CONFIG["email"]).strip()
    if _is_placeholder_email(raw_email):
        # Unpaywall ALWAYS requires a real email and rejects placeholders with
        # 422. Ask the user for theirs instead of silently losing the channel.
        return fail(
            doi,
            "Unpaywall needs the user's real email address — only a placeholder is configured",
            error_type="config_needed",
            action="ask_user_email",
        )
    email = urllib.parse.quote(raw_email)
    q = urllib.parse.quote(doi, safe="")
    url = f"https://api.unpaywall.org/v2/{q}?email={email}"
    try:
        resp = fetch(url, config, headers={"Accept": "application/json"})
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        status = 0
    if status == 422:
        return fail(
            doi,
            "Unpaywall rejected the configured email address (HTTP 422)",
            error_type="config_needed",
            action="ask_user_email",
        )
    if status == 429:
        # Temporary, per-IP limit — retry later rather than dropping the channel.
        return fail(
            doi,
            "Unpaywall rate limit (HTTP 429) — temporary, retry later",
            error_type="rate_limited",
            action="retry_later",
            extra={"status_code": 429},
        )
    if status != 200:
        # 404 = DOI genuinely not in Unpaywall (silent skip is correct);
        # other transient errors just let the race continue elsewhere.
        return None
    try:
        payload = resp.json()
    except Exception:
        return None

    candidates = extract_unpaywall_pdf_candidates(payload)
    for pdf_url in candidates[: int(config.get("max_unpaywall_candidates", 2))]:
        polite_delay(config)
        result = download_pdf(pdf_url, output_path, config, "Unpaywall")
        if result:
            result["doi"] = doi
            result["identifier"] = doi
            return result
    return None
