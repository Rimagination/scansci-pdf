"""arXiv metadata fetch + PDF download — thin API for PaperFetcher."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import requests

from ...identifiers import normalize_arxiv_id

logger = logging.getLogger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"


def extract_arxiv_id(text: str) -> str | None:
    """Extract arXiv ID from a URL or string."""
    # Try to use the existing normalizer first
    result = normalize_arxiv_id(text)
    if result:
        return result
    # Fallback: regex
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", text)
    return m.group(1) + (m.group(2) or "") if m else None


def fetch_metadata(arxiv_id: str) -> dict[str, Any] | None:
    """Fetch metadata for an arXiv paper via Atom API."""
    try:
        resp = requests.get(
            ARXIV_API,
            params={"id_list": arxiv_id, "max_results": 1},
            timeout=30,
            headers={"User-Agent": "scansci-pdf/1.5"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("arXiv API failed for %s: %s", arxiv_id, e)
        return None

    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return None

        title_el = entry.find("atom:title", ns)
        title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""

        authors = []
        for author_el in entry.findall("atom:author", ns):
            name_el = author_el.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        summary_el = entry.find("atom:summary", ns)
        abstract = summary_el.text.strip() if summary_el is not None and summary_el.text else ""

        published_el = entry.find("atom:published", ns)
        year = None
        if published_el is not None and published_el.text:
            m = re.match(r"(\d{4})", published_el.text)
            if m:
                year = int(m.group(1))

        link_el = entry.find("atom:id", ns)
        url = link_el.text.strip() if link_el is not None and link_el.text else ""

        return {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "year": year,
            "url": url,
        }
    except Exception as e:
        logger.warning("Failed to parse arXiv response: %s", e)
        return None


def download_pdf(arxiv_id: str, output_path: str) -> bool:
    """Download arXiv PDF to output_path."""
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    try:
        resp = requests.get(url, timeout=60, stream=True, headers={"User-Agent": "scansci-pdf/1.5"})
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "").lower()
        if "pdf" not in ct:
            logger.warning("arXiv PDF URL returned non-PDF content: %s", ct)
            return False
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except requests.RequestException as e:
        logger.warning("arXiv PDF download failed: %s", e)
        return False
