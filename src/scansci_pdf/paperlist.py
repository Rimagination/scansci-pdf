"""Paper list parser for APA references, BibTeX, and DOI lists."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identifiers import normalize_doi


@dataclass
class PaperEntry:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    journal: str | None = None
    raw: str = ""


def parse_paper_list(file_path: str | Path) -> list[PaperEntry]:
    """Auto-detect format and parse paper list."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = path.suffix.lower()
    if suffix in (".csv", ".xlsx", ".tsv", ".tab"):
        from .pipeline import entries_from_table, read_table

        return _entries_from_queue(entries_from_table(read_table(path)))

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="latin-1")

    if suffix == ".bib":
        return _parse_bib_to_entries(content)
    if suffix in (".txt", ".md"):
        # Try APA format first (has author-year patterns)
        if re.search(r"[A-Z][a-z\u00C0-\u024F]+,\s+[A-Z]\.", content):
            return parse_apa_references(content)
        # Fall back to DOI list
        return _parse_doi_list(content)

    # Default: try APA, then DOI list
    if re.search(r"[A-Z][a-z\u00C0-\u024F]+,\s+[A-Z]\.", content):
        return parse_apa_references(content)
    return _parse_doi_list(content)


def _entries_from_queue(queue_entries) -> list[PaperEntry]:
    """Convert pipeline QueueEntry objects to PaperEntry (skipping unresolved)."""
    out = []
    for e in queue_entries:
        if e.unresolved or not e.identifier:
            continue
        out.append(PaperEntry(title=e.title, doi=e.identifier, raw=e.raw or e.identifier))
    return out


def parse_apa_references(text: str) -> list[PaperEntry]:
    """Parse APA-style reference list."""
    entries = []
    raw_entries = _split_apa_entries(text)
    for raw in raw_entries:
        entry = _parse_single_apa(raw)
        if entry:
            entries.append(entry)
    return entries


def _split_apa_entries(text: str) -> list[str]:
    """Split continuous APA text by DOI URL boundaries.

    Each APA entry ends with a DOI URL. Find DOI URLs and use them as
    entry boundaries, assigning each DOI URL to the following entry.
    """
    # Find all DOI URL positions
    doi_positions = [m.end() for m in re.finditer(r"https://doi\.org/\S+", text)]
    if not doi_positions:
        return [text.strip()] if text.strip() else []

    # Split after each DOI URL
    parts = []
    for i, end_pos in enumerate(doi_positions):
        start = doi_positions[i - 1] if i > 0 else 0
        # Find the next author pattern after this DOI
        rest = text[end_pos:]
        # Skip whitespace between DOI and next entry
        m = re.match(r"\s+", rest)
        split_pos = end_pos + (m.end() if m else 0)
        parts.append(text[start:split_pos].strip())

    # Add any remaining text after the last DOI
    last_end = doi_positions[-1]
    rest = text[last_end:]
    m = re.match(r"\s+", rest)
    remaining_start = last_end + (m.end() if m else 0)
    if remaining_start < len(text):
        remaining = text[remaining_start:].strip()
        if remaining:
            parts.append(remaining)

    return [p for p in parts if p]


def _parse_single_apa(raw: str) -> PaperEntry | None:
    """Parse a single APA reference entry."""
    if len(raw) < 20:
        return None

    # Extract year
    year_match = re.search(r"\((\d{4})[a-z]?\)", raw)
    year = int(year_match.group(1)) if year_match else None

    # Extract DOI
    doi = None
    doi_match = re.search(r"https?://doi\.org/(\S+)", raw)
    if doi_match:
        doi = _normalize_doi_string(doi_match.group(1))

    # Extract title: between year and journal (or volume number)
    title = ""
    if year_match:
        after_year = raw[year_match.end():].strip()
        # Skip leading period/punctuation after year
        after_year = after_year.lstrip(". ")
        # Title ends at period before journal name
        title_match = re.match(r"(.+?)\.\s+", after_year)
        if title_match:
            title = title_match.group(1).strip()

    # Extract first author
    authors = []
    author_match = re.match(r"([A-Z\u00C0-\u024F][a-z\u00C0-\u024F-]+,\s+[A-Z]\.[^,(&]*)", raw)
    if author_match:
        authors = [author_match.group(1).strip()]

    return PaperEntry(
        title=title,
        authors=authors,
        year=year,
        doi=doi,
        raw=raw[:200],
    )


def _normalize_doi_string(doi: str) -> str | None:
    """Fix DOI format: unicode hyphens to ASCII, remove internal spaces."""
    if not doi:
        return None
    # Unicode hyphens → ASCII
    doi = doi.replace("\u2010", "-")  # HYPHEN
    doi = doi.replace("\u2012", "-")  # FIGURE DASH
    doi = doi.replace("\u2013", "-")  # EN DASH
    doi = doi.replace("\u2014", "-")  # EM DASH
    # Remove internal spaces
    doi = re.sub(r"\s+", "", doi)
    # Strip trailing punctuation
    doi = doi.rstrip(".,;:")
    # Strip trailing author names (e.g. "10.1002/ird.2673Hamed" → "10.1002/ird.2673")
    # Author names start with uppercase letter followed by lowercase letters
    doi = re.sub(r"[A-Z][a-z\u00C0-\u024F]{1,}$", "", doi)
    # Validate
    if re.match(r"^10\.\d{4,}/", doi):
        return doi
    return None


def _parse_bib_to_entries(content: str) -> list[PaperEntry]:
    """Parse BibTeX content into PaperEntry list."""
    from .bibparser import parse_bib_text
    raw_entries = parse_bib_text(content, require_doi=False)
    entries = []
    for e in raw_entries:
        doi = e.get("doi", "")
        if doi:
            doi = _normalize_doi_string(doi)
        authors_str = e.get("author", e.get("authors", ""))
        authors = [a.strip() for a in authors_str.split(" and ")] if authors_str else []
        year_str = e.get("year", "")
        year = int(year_str) if year_str.isdigit() else None
        entries.append(PaperEntry(
            title=e.get("title", ""),
            authors=authors,
            year=year,
            doi=doi,
            journal=e.get("journal", ""),
            raw=f"@{e.get('type', 'article')}{{{e.get('cite_key', '')}",
        ))
    return entries


def _parse_doi_list(content: str) -> list[PaperEntry]:
    """Parse plain text DOI list."""
    entries = []
    for part in re.split(r"[,\n;]+", content):
        doi = part.strip()
        if re.match(r"^10\.\d{4,}/", doi):
            entries.append(PaperEntry(doi=doi, raw=doi))
    return entries
