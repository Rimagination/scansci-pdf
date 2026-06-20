"""Elsevier RetrievalAPI integration for fetching full-text articles."""

import logging
import os
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger(__name__)

ELSEVIER_API = "https://api.elsevier.com/content"


def get_api_key(config_key: str = "") -> str:
    """Get Elsevier API key from config or environment."""
    return config_key or os.environ.get("ELSEVIER_API_KEY", "")


def fetch_pdf(doi: str, api_key: str, inst_token: str = "") -> bytes | None:
    """Download full PDF via Elsevier API using the attachment EID approach.

    Strategy (from successful 32-paper batch experience):
    1. GET /article/doi/{doi}?view=FULL → XML with attachment metadata
    2. Parse XML to find MAIN PDF attachment-eid (main.pdf or mainext.pdf)
    3. GET /object/eid/{attachment-eid} → official publisher PDF

    This bypasses ScienceDirect web pages, Cloudflare, and CAPTCHAs entirely.
    Direct PDF endpoint (/article/doi/{doi} + Accept: application/pdf) only
    returns a 1-page preview for paywalled articles.
    """
    if not api_key:
        return None

    # Step 1: Get FULL XML with attachment metadata
    eids = _fetch_attachment_eids(doi, api_key, inst_token)
    if not eids:
        # Fallback: try direct PDF endpoint (works for OA articles)
        return _fetch_pdf_direct(doi, api_key, inst_token)

    # Step 2: Try each attachment EID until we get a valid PDF
    for eid in eids:
        pdf_bytes = _fetch_pdf_by_eid(eid, api_key, inst_token)
        if pdf_bytes:
            return pdf_bytes

    logger.info("Elsevier API: all attachment EIDs failed for %s, trying direct", doi)
    return _fetch_pdf_direct(doi, api_key, inst_token)


def _fetch_attachment_eids(doi: str, api_key: str, inst_token: str = "") -> list[str]:
    """Fetch FULL XML and extract MAIN PDF attachment EIDs."""
    url = f"{ELSEVIER_API}/article/doi/{doi}?view=FULL"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/xml",
    }
    if inst_token:
        headers["X-ELS-InstToken"] = inst_token

    resp = _api_request(url, headers)
    if not resp or resp.status_code != 200:
        return []

    # Parse XML to find attachment EIDs
    eids = []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        for el in root.iter():
            local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if local == "web-pdf":
                eid = ""
                purpose = ""
                page_count = 0
                for child in el:
                    child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if child_local == "attachment-eid" and child.text:
                        eid = child.text.strip()
                    elif child_local == "web-pdf-purpose" and child.text:
                        purpose = child.text.strip()
                    elif child_local == "web-pdf-page-count" and child.text:
                        try:
                            page_count = int(child.text.strip())
                        except ValueError:
                            pass
                if eid and purpose == "MAIN":
                    eids.append(eid)
                    logger.info("Elsevier API: found MAIN attachment %s (%d pages)", eid, page_count)
    except Exception as e:
        logger.warning("Failed to parse Elsevier XML attachments: %s", e)

    # Sort: prefer main.pdf over mainext.pdf
    eids.sort(key=lambda e: (0 if e.endswith("-main.pdf") else 1))
    return eids


def _fetch_pdf_by_eid(eid: str, api_key: str, inst_token: str = "") -> bytes | None:
    """Download PDF via Content Object API using attachment EID."""
    url = f"{ELSEVIER_API}/object/eid/{eid}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/pdf",
    }
    if inst_token:
        headers["X-ELS-InstToken"] = inst_token

    resp = _api_request(url, headers)
    if not resp:
        return None

    if resp.status_code == 404:
        logger.info("Elsevier API: attachment %s not found (404)", eid)
        return None

    if resp.status_code != 200:
        logger.info("Elsevier API: HTTP %d for attachment %s", resp.status_code, eid)
        return None

    if resp.content[:5] != b"%PDF-":
        logger.info("Elsevier API: attachment %s returned non-PDF", eid)
        return None

    if len(resp.content) < 10000:
        logger.warning("Elsevier API: attachment %s too small (%d bytes)", eid, len(resp.content))
        return None

    logger.info("Elsevier API: downloaded %d bytes via attachment %s", len(resp.content), eid)
    return resp.content


def _fetch_pdf_direct(doi: str, api_key: str, inst_token: str = "") -> bytes | None:
    """Fallback: download PDF directly from article endpoint (works for OA)."""
    url = f"{ELSEVIER_API}/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/pdf",
    }
    if inst_token:
        headers["X-ELS-InstToken"] = inst_token

    resp = _api_request(url, headers)
    if not resp or resp.status_code != 200:
        return None

    content_type = resp.headers.get("content-type", "")
    if "pdf" not in content_type and resp.content[:5] != b"%PDF-":
        logger.info("Elsevier API: direct endpoint returned non-PDF (%s)", content_type[:50])
        return None

    if len(resp.content) < 10000:
        logger.warning("Elsevier API: direct PDF too small (%d bytes)", len(resp.content))
        return None

    logger.info("Elsevier API: downloaded %d bytes directly for %s", len(resp.content), doi)
    return resp.content


def _api_request(url: str, headers: dict) -> requests.Response | None:
    """Make an Elsevier API request with error handling."""
    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.get(url, headers=headers, timeout=30, allow_redirects=True)
    except requests.exceptions.SSLError:
        try:
            session = requests.Session()
            session.trust_env = False
            resp = session.get(url, headers=headers, timeout=30, allow_redirects=True, verify=False)
        except requests.RequestException as e:
            logger.warning("Elsevier API request failed: %s", e)
            return None
    except requests.RequestException as e:
        logger.warning("Elsevier API request failed: %s", e)
        return None

    if resp.status_code in (401, 403):
        logger.warning("Elsevier API: HTTP %d (key invalid or insufficient)", resp.status_code)
    elif resp.status_code == 429:
        logger.warning("Elsevier API: rate limited")

    return resp


def fetch_fulltext(doi: str, api_key: str, inst_token: str = "") -> dict | None:
    """Fetch article full text via Elsevier RetrievalAPI."""
    if not api_key:
        return None

    url = f"{ELSEVIER_API}/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/xml",
    }
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token

    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.get(url, headers=headers, timeout=30, allow_redirects=True)
    except requests.exceptions.SSLError:
        try:
            resp = session.get(url, headers=headers, timeout=30, allow_redirects=True, verify=False)
        except requests.RequestException as e:
            logger.warning("Elsevier API request failed: %s", e)
            return None
    except requests.RequestException as e:
        logger.warning("Elsevier API request failed: %s", e)
        return None

    if resp.status_code == 401:
        logger.warning("Elsevier API: invalid API key")
        return None
    if resp.status_code == 404:
        logger.info("Elsevier API: DOI %s not found", doi)
        return None
    if resp.status_code != 200:
        logger.info("Elsevier API: HTTP %d for %s", resp.status_code, doi)
        return None

    return _parse_xml(resp.text)


def _parse_xml(xml_text: str) -> dict | None:
    """Parse Elsevier XML response into structured data."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Failed to parse Elsevier XML: %s", e)
        return None

    result = {
        "title": "",
        "authors": [],
        "abstract": "",
        "full_text": "",
        "figures": [],
        "references": [],
    }

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "title" and el.text and el.text.strip():
            result["title"] = el.text.strip()
            break

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("creator", "author"):
            if el.text and el.text.strip():
                result["authors"].append(el.text.strip())
    if not result["authors"]:
        for el in root.iter():
            local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if local == "author":
                given = ""
                surname = ""
                for child in el:
                    child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if "given" in child_local:
                        given = (child.text or "").strip()
                    elif "surname" in child_local or "last" in child_local:
                        surname = (child.text or "").strip()
                if given or surname:
                    result["authors"].append(f"{given} {surname}".strip())

    result["abstract"] = _extract_abstract(root)
    result["full_text"] = _extract_body(root)
    result["references"] = _extract_references(root)

    return result


def _extract_abstract(root: ET.Element) -> str:
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("abstract", "description") and _collect_text(el).strip():
            return _collect_text(el).strip()
    return ""


def _extract_body(root: ET.Element) -> str:
    parts = []

    body = None
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "body":
            body = el
            break

    if body is None:
        return ""

    def _find_sections(el):
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "section":
            yield el
        for child in el:
            yield from _find_sections(child)

    for section in _find_sections(body):
        heading = ""
        content_parts = []

        for child in section:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag in ("section-title", "sectiontitle", "heading"):
                heading = _collect_text(child).strip()
            elif tag == "para":
                text = _collect_text(child).strip()
                if text:
                    content_parts.append(text)

        if heading and content_parts:
            parts.append(f"## {heading}\n\n{' '.join(content_parts)}")
        elif content_parts:
            parts.append(" ".join(content_parts))

    if not parts:
        for child in body.iter():
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "para":
                text = _collect_text(child).strip()
                if text:
                    parts.append(text)

    return "\n\n".join(parts)


def _extract_references(root: ET.Element) -> list[str]:
    refs = []

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("bib-reference", "reference"):
            text = " ".join(_collect_text(el).split())
            if text and len(text) > 10:
                refs.append(text)

    if refs:
        return refs

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "bibliography":
            for ref in el:
                text = " ".join(_collect_text(ref).split())
                if text and len(text) > 10:
                    refs.append(text)
            if refs:
                return refs

    return refs


def _collect_text(el: ET.Element) -> str:
    parts = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_collect_text(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(parts)
