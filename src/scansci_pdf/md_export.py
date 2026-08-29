"""Agent-ready markdown export: PDF in, clean markdown out.

The default deliverable stays the PDF (human-readable); this module adds an
optional AI reading layer on top of the downloaded file via pymupdf4llm.
"""

from __future__ import annotations

from pathlib import Path


def pdf_to_markdown(pdf_path: str | Path, write: bool = True) -> str | Path:
    """Convert a downloaded PDF to markdown alongside it.

    Returns the written .md path (write=True) or the markdown text.
    Raises a RuntimeError with an install hint when pymupdf4llm is missing.
    """
    try:
        import pymupdf4llm
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "markdown 导出需要 pymupdf4llm：pip install pymupdf4llm"
        ) from exc

    src = Path(pdf_path)
    md_text = pymupdf4llm.to_markdown(str(src))
    if not write:
        return md_text
    out = src.with_suffix(".md")
    out.write_text(md_text, encoding="utf-8")
    return out
