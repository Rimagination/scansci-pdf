"""Unpaywall OA detection — thin API for PaperFetcher."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

UNPAYWALL_API = "https://api.unpaywall.org/v2"


@dataclass
class OAResult:
    """Result from Unpaywall OA check."""
    doi: str = ""
    is_oa: bool = False
    title: str = ""
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: int | None = None
    source: str = ""
    pdf_url: str = ""
    html_url: str = ""


def check_oa(doi: str, email: str = "") -> OAResult:
    """Check if a DOI has an Open Access version via Unpaywall."""
    url = f"{UNPAYWALL_API}/{doi}"
    params = {"email": email or "scansci-pdf@example.invalid"}

    try:
        resp = requests.get(url, params=params, timeout=15, headers={"User-Agent": "scansci-pdf/1.5"})
        if resp.status_code == 404:
            logger.info("Unpaywall: DOI %s not found", doi)
            return OAResult(doi=doi)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Unpaywall API failed for %s: %s", doi, e)
        return OAResult(doi=doi)

    data = resp.json()
    result = OAResult(
        doi=doi,
        is_oa=bool(data.get("is_oa")),
        title=data.get("title") or "",
        journal=data.get("journal_name") or "",
    )

    # Year
    year_val = data.get("year")
    if year_val:
        try:
            result.year = int(year_val)
        except (ValueError, TypeError):
            pass

    # Authors
    for author in data.get("z_authors") or []:
        if isinstance(author, dict):
            given = author.get("given", "")
            family = author.get("family", "")
            if given or family:
                result.authors.append(f"{given} {family}".strip())

    # Best OA location
    best = data.get("best_oa_location")
    if isinstance(best, dict):
        result.pdf_url = best.get("url_for_pdf") or ""
        result.html_url = best.get("url_for_landing_page") or ""
        result.source = best.get("host_type") or ""

    # Fallback: check all OA locations
    if not result.pdf_url:
        for loc in data.get("oa_locations") or []:
            if not isinstance(loc, dict):
                continue
            pdf = loc.get("url_for_pdf") or ""
            if pdf:
                result.pdf_url = pdf
                result.source = loc.get("host_type") or ""
                break

    return result
