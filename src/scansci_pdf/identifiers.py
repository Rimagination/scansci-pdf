"""DOI and arXiv identifier parsing."""

from __future__ import annotations

import re
import urllib.parse

ARXIV_RE = re.compile(r"^(?:arxiv:)?(?P<id>\d{4}\.\d{4,5})(?:v\d+)?$", re.I)
ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.(?P<id>\d{4}\.\d{4,5})(?:v\d+)?", re.I)
OLD_ARXIV_RE = re.compile(r"^(?:arxiv:)?(?P<id>[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)


def normalize_doi(value: str) -> str:
    value = value.strip()
    # Chinese users paste DOIs from Word/PDF with full-width digits, slashes,
    # and trailing Chinese punctuation (１０．３９６９／…。).  NFKC + the
    # explicit cleanup below makes those usable without special-casing every
    # caller.
    from .chinese import to_halfwidth

    value = to_halfwidth(value)
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi\s*[:：]\s*", "", value, flags=re.I)
    value = re.sub(r"[。，；：、]+$", "", value).strip()
    return urllib.parse.unquote(value).strip()


def is_chinese_doi(value: str) -> bool:
    """Whether a DOI belongs to a Chinese registration agency/pattern."""
    from .chinese import is_chinese_doi as _is_chinese_doi

    return _is_chinese_doi(value)


def normalize_arxiv_id(value: str) -> str | None:
    raw = value.strip()
    lower = raw.lower()
    if lower.startswith("http://arxiv.org/abs/") or lower.startswith("https://arxiv.org/abs/"):
        raw = raw.rsplit("/", 1)[-1]
    elif lower.startswith("http://arxiv.org/pdf/") or lower.startswith("https://arxiv.org/pdf/"):
        raw = raw.rsplit("/", 1)[-1].removesuffix(".pdf")

    doi_match = ARXIV_DOI_RE.search(raw)
    if doi_match:
        return doi_match.group("id")

    match = ARXIV_RE.match(raw)
    if match:
        return match.group("id")

    old_match = OLD_ARXIV_RE.match(raw)
    if old_match:
        return old_match.group("id")

    return None


def is_arxiv_identifier(value: str) -> bool:
    return normalize_arxiv_id(value) is not None


def normalize_doi_unicode(value: str) -> str | None:
    """Fix DOI with unicode hyphens and embedded spaces. Returns None if invalid."""
    doi = value.strip()
    # Unicode hyphens → ASCII
    doi = doi.replace("\u2010", "-")  # HYPHEN
    doi = doi.replace("\u2012", "-")  # FIGURE DASH
    doi = doi.replace("\u2013", "-")  # EN DASH
    doi = doi.replace("\u2014", "-")  # EM DASH
    # Remove internal spaces (from PDF copy-paste line breaks)
    doi = re.sub(r"\s+", "", doi)
    # Validate
    if re.match(r"^10\.\d{4,}/", doi):
        return doi
    return None


def validate_doi(doi: str) -> tuple[bool, str]:
    """Check if a DOI resolves via doi.org. Returns (valid, resolved_url or error).

    Chinese-registered DOIs (10.3969 / CNKI) are accepted without a doi.org
    HEAD check: multi-resolution handles frequently answer 404/600 to HEAD
    even though the DOI is valid, and the CN source lanes validate access
    themselves.
    """
    if is_chinese_doi(doi):
        return True, "Chinese DOI (validated by CN sources)"

    import requests

    from .network import thread_session
    try:
        # Batches validate up to ten DOIs at once, so a module-level request
        # opened a connection per call and one shared session would be reached
        # from ten threads at a time (#72).
        resp = thread_session().head(
            f"https://doi.org/{urllib.parse.quote(doi, safe='')}",
            timeout=10,
            allow_redirects=True,
            headers={"User-Agent": "scansci-pdf/1.1"},
        )
        if resp.status_code == 200:
            return True, resp.url
        elif resp.status_code == 404:
            return False, "DOI not found (404)"
        else:
            return True, f"status={resp.status_code}"
    except requests.Timeout:
        return True, "timeout (assume valid)"
    except Exception as e:
        return True, f"check failed: {e}"


def safe_filename(identifier: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", identifier).strip("_") or "paper"
