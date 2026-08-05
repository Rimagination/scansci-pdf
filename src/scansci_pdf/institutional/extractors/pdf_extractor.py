"""PDF text extraction using pymupdf."""

import logging
import re
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

# Patterns for figure captions
FIGURE_PATTERN = re.compile(
    r"^(?:Fig(?:ure|\.)\s*\d+[.:]\s*.+)$",
    re.MULTILINE | re.IGNORECASE,
)


def extract_text(pdf_path: str | Path) -> str:
    """Extract text content from a PDF file.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Extracted text content.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.error("PDF file not found: %s", pdf_path)
        return ""

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:
        logger.error("Failed to open PDF %s: %s", pdf_path, e)
        return ""

    text_parts = []
    for page in doc:
        text = page.get_text("text")
        if text:
            text_parts.append(text)

    doc.close()
    full_text = "\n\n".join(text_parts)

    # Clean up common PDF artifacts
    full_text = _clean_text(full_text)
    return full_text


def extract_figures(pdf_path: str | Path) -> list[str]:
    """Extract figure captions from a PDF file.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        List of figure caption strings.
    """
    text = extract_text(pdf_path)
    if not text:
        return []

    captions = []
    for match in FIGURE_PATTERN.finditer(text):
        caption = match.group(0).strip()
        if len(caption) > 10:  # Filter out too-short matches
            captions.append(caption)

    return captions


def extract_figures_from_text(text: str) -> list[str]:
    """Extract figure captions from already-extracted text.

    Args:
        text: Extracted text content from a PDF.

    Returns:
        List of figure caption strings.
    """
    if not text:
        return []

    captions = []
    for match in FIGURE_PATTERN.finditer(text):
        caption = match.group(0).strip()
        if len(caption) > 10:
            captions.append(caption)

    return captions


def extract_from_bytes(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes (e.g., from a download).

    Args:
        pdf_bytes: Raw PDF content.

    Returns:
        Extracted text content.
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.error("Failed to open PDF from bytes: %s", e)
        return ""

    text_parts = []
    for page in doc:
        text = page.get_text("text")
        if text:
            text_parts.append(text)

    doc.close()
    return _clean_text("\n\n".join(text_parts))


def _clean_text(text: str) -> str:
    """Clean up common PDF extraction artifacts."""
    # Remove excessive whitespace but preserve paragraph breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove hyphenation at line breaks
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Fix broken lines within paragraphs (single newline -> space)
    # But preserve double newlines (paragraph breaks)
    _SENTENCE_ENDS = (".", ":", "?", "!", "。", "；", "！", "？")
    _LIST_PREFIX = re.compile(r"^\s*(?:\d+[\.\)]\s|[-*•]\s)")
    lines = text.split("\n")
    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            result.append("")
        elif (
            i + 1 < len(lines)
            and lines[i + 1].strip()
            and not stripped.endswith(_SENTENCE_ENDS)
            and len(stripped) > 20
            and not _LIST_PREFIX.match(stripped)
            and not _LIST_PREFIX.match(lines[i + 1].strip())
        ):
            result.append(stripped + " ")
        else:
            result.append(stripped + "\n")

    return "".join(result).strip()
