"""Citation export in multiple formats (BibTeX, RIS, EndNote)."""

from __future__ import annotations

import re
from typing import Any

from .network import fetch_json


def fetch_metadata(doi: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch paper metadata from Crossref, with a Chinese-source fallback.

    Chinese DOIs registered through ISTIC/China DOI or CNKI are not in
    Crossref.  For those, resolve the DOI and enrich from NCPSSD/chndoi/Wanfang
    instead so auto-rename, BibTeX, and citation export keep working.
    """
    import urllib.parse
    q = urllib.parse.quote(doi, safe="")
    url = f"https://api.crossref.org/works/{q}"
    try:
        payload = fetch_json(url, config, headers={"Accept": "application/json"})
        if payload:
            message = payload.get("message")
            if message:
                return message
    except Exception:
        pass

    try:
        from .chinese import is_chinese_doi
        if is_chinese_doi(doi):
            from .sources.ncpssd import chinese_metadata
            return chinese_metadata(doi, config)
    except Exception:
        pass
    return None


def _extract_fields(msg: dict[str, Any], doi: str) -> dict[str, str]:
    """Extract common fields from Crossref metadata."""
    fields: dict[str, str] = {}

    # Title
    titles = msg.get("title", [])
    if titles:
        fields["title"] = _clean_html(titles[0])

    # Authors
    authors = msg.get("author", [])
    if authors:
        fields["authors"] = authors

    # Journal
    container = msg.get("container-title", [])
    if container:
        fields["journal"] = _clean_html(container[0])

    # Year
    parts = msg.get("published-print", msg.get("published-online", msg.get("created", {})))
    date_parts = parts.get("date-parts", [[]]) if isinstance(parts, dict) else [[]]
    if date_parts and date_parts[0]:
        fields["year"] = str(date_parts[0][0])

    # Volume, Issue, Pages
    if msg.get("volume"):
        fields["volume"] = str(msg["volume"])
    if msg.get("issue"):
        fields["issue"] = str(msg["issue"])
    if msg.get("page"):
        fields["pages"] = msg["page"]

    fields["doi"] = doi
    fields["url"] = f"https://doi.org/{doi}"

    if msg.get("publisher"):
        fields["publisher"] = _clean_html(msg["publisher"])

    if msg.get("abstract"):
        fields["abstract"] = _clean_html(msg["abstract"])

    # Entry type
    type_mapping = {
        "journal-article": "JOUR",
        "proceedings-article": "CONF",
        "book-chapter": "CHAP",
        "book": "BOOK",
        "monograph": "BOOK",
        "dissertation": "THES",
        "report": "RPRT",
        "posted-content": "UNPB",
    }
    fields["entry_type"] = type_mapping.get(msg.get("type", ""), "JOUR")

    return fields


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _format_authors_ris(authors: list[dict]) -> list[str]:
    """Format authors for RIS format."""
    result = []
    for a in authors:
        family = a.get("family", "")
        given = a.get("given", "")
        if family:
            result.append(f"{family}, {given}".strip(", "))
    return result


def to_ris(doi: str, config: dict[str, Any]) -> str | None:
    """Generate RIS format citation."""
    msg = fetch_metadata(doi, config)
    if not msg:
        return None
    return _metadata_to_ris(msg, doi)


def _metadata_to_ris(msg: dict[str, Any], doi: str) -> str:
    fields = _extract_fields(msg, doi)
    lines = []

    # Type
    lines.append(f"TY  - {fields.get('entry_type', 'JOUR')}")

    # Authors
    authors = fields.get("authors", [])
    for a in _format_authors_ris(authors):
        lines.append(f"AU  - {a}")

    # Title
    if "title" in fields:
        lines.append(f"TI  - {fields['title']}")

    # Journal
    if "journal" in fields:
        lines.append(f"JO  - {fields['journal']}")

    # Year
    if "year" in fields:
        lines.append(f"PY  - {fields['year']}")

    # Volume
    if "volume" in fields:
        lines.append(f"VL  - {fields['volume']}")

    # Issue
    if "issue" in fields:
        lines.append(f"IS  - {fields['issue']}")

    # Pages
    if "pages" in fields:
        pages = fields["pages"]
        if "-" in pages:
            parts = pages.split("-", 1)
            lines.append(f"SP  - {parts[0].strip()}")
            lines.append(f"EP  - {parts[1].strip()}")
        else:
            lines.append(f"SP  - {pages}")

    # DOI
    lines.append(f"DO  - {doi}")

    # URL
    lines.append(f"UR  - https://doi.org/{doi}")

    # Publisher
    if "publisher" in fields:
        lines.append(f"PB  - {fields['publisher']}")

    # Abstract
    if "abstract" in fields:
        lines.append(f"AB  - {fields['abstract']}")

    lines.append("ER  - ")
    return "\n".join(lines)


def to_endnote(doi: str, config: dict[str, Any]) -> str | None:
    """Generate EndNote format citation."""
    msg = fetch_metadata(doi, config)
    if not msg:
        return None
    return _metadata_to_endnote(msg, doi)


def _metadata_to_endnote(msg: dict[str, Any], doi: str) -> str:
    fields = _extract_fields(msg, doi)
    lines = []

    # Type
    lines.append(f"%0 {fields.get('entry_type', 'JOUR')}")

    # Authors
    authors = fields.get("authors", [])
    for a in _format_authors_ris(authors):
        lines.append(f"%A {a}")

    # Title
    if "title" in fields:
        lines.append(f"%T {fields['title']}")

    # Journal
    if "journal" in fields:
        lines.append(f"%J {fields['journal']}")

    # Year
    if "year" in fields:
        lines.append(f"%D {fields['year']}")

    # Volume
    if "volume" in fields:
        lines.append(f"%V {fields['volume']}")

    # Issue
    if "issue" in fields:
        lines.append(f"%N {fields['issue']}")

    # Pages
    if "pages" in fields:
        lines.append(f"%P {fields['pages']}")

    # DOI
    lines.append(f"%R {doi}")

    # URL
    lines.append(f"%U https://doi.org/{doi}")

    # Publisher
    if "publisher" in fields:
        lines.append(f"%I {fields['publisher']}")

    # Abstract
    if "abstract" in fields:
        lines.append(f"%X {fields['abstract']}")

    return "\n".join(lines)
