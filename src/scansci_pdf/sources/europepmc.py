"""Europe PMC source for life sciences papers."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from ..network import fetch_json, polite_delay
from ..pdf_utils import download_pdf, is_plausible_pdf_url, dedupe


def extract_europepmc_pdf_candidates(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    results = payload.get("resultList", {}).get("result", [])
    if not isinstance(results, list):
        results = []
    for result in results:
        fulltext = result.get("fullTextUrlList", {}) if isinstance(result, dict) else {}
        urls = fulltext.get("fullTextUrl", []) if isinstance(fulltext, dict) else []
        if not isinstance(urls, list):
            urls = [urls]
        for entry in urls:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url", "")
            style = str(entry.get("documentStyle", "")).lower()
            if style == "pdf" or is_plausible_pdf_url(url):
                if is_plausible_pdf_url(url):
                    candidates.append(url)
    return dedupe(candidates)


def try_europepmc(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    q = urllib.parse.quote(f"DOI:{doi}")
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={q}&format=json&resultType=core&pageSize=5"
    payload = fetch_json(url, config)
    if not payload:
        return None

    candidates = extract_europepmc_pdf_candidates(payload)
    for pdf_url in candidates[: int(config.get("max_europepmc_candidates", 3))]:
        polite_delay(config)
        result = download_pdf(pdf_url, output_path, config, "EuropePMC")
        if result:
            result["doi"] = doi
            result["identifier"] = doi
            return result
    return None


def try_pmc(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try PubMed Central for free full-text PDF via Europe PMC render URL."""
    search_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={doi}&format=json"
    try:
        resp_data = fetch_json(search_url, config)
        if not resp_data:
            return None
        records = resp_data.get("records", [])
        pmcid = None
        for rec in records:
            if rec.get("pmcid"):
                pmcid = rec["pmcid"]
                break
        if not pmcid:
            return None
    except Exception:
        return None
    # Use Europe PMC render URL — NCBI's /pdf/ returns HTML with JS redirect
    pdf_url = f"https://europepmc.org/articles/{pmcid}?pdf=render"
    result = download_pdf(pdf_url, output_path, config, f"PMC({pmcid})")
    if result:
        result["doi"] = doi
        result["identifier"] = doi
    return result
