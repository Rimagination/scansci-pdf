"""Agent-ready markdown export: PDF in, clean markdown out.

The default deliverable stays the PDF (human-readable); this module adds an
optional AI reading layer on top of the downloaded file via pymupdf4llm.

Post-processing repairs two deterministic classes of pymupdf4llm artifacts
before returning the text:

- Decomposed accents rendered as literal marks, e.g. ``Daniel S<sup>ˇ</sup>uta``
  for ``Daniel Šuta`` — re-folded into combining characters, then NFC-normalized.
- Unicode replacement characters (``�``, U+FFFD) from lost font glyphs, which
  cannot be recovered; they are counted and reported as a quality warning.

``markdown_warnings`` lets agents know the text is readable but names/symbols
may need spot-checking, without blocking the export.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Diacritic marks that pymupdf4llm sometimes wraps in <sup> when the PDF font
# decomposed them: <sup>ˇ</sup> -> U+030C (combining caron), etc. NFC then
# merges the combining char into the preceding letter (S + U+030C -> Š).
_SUP_DIACRITIC_RE = re.compile(r"<sup>\s*([ˇˆ´`¨¯˙˝˘˜])\s*</sup>")

_DIACRITIC_TO_COMBINING = {
    "ˇ": "\u030c",  # caron
    "ˆ": "\u0302",  # circumflex
    "´": "\u0301",  # acute
    "`": "\u0300",  # grave
    "¨": "\u0308",  # diaeresis
    "¯": "\u0304",  # macron
    "˙": "\u0307",  # dot above
    "˝": "\u030b",  # double acute
    "˘": "\u0306",  # breve
    "˜": "\u0303",  # tilde
}

# After folding, a stray space split the accent from its letter
# ("S<sup>ˇ</sup> uta" -> "S\u030c uta"); drop the single space so NFC merges.
_COMBINING_SPACE_RE = re.compile(r"([\u0300-\u036f]) +(?=[A-Za-z])")

_REPLACEMENT_CHAR_RE = re.compile(r"\ufffd")
_NONPRINTABLE_RE = re.compile(r"[^\x09\x0a\x0d\x20-\x7e\u00a0-\uffff]")


def clean_markdown_text(md_text: str) -> str:
    """Repair deterministic pymupdf4llm artifacts (see module docstring)."""
    text = _SUP_DIACRITIC_RE.sub(
        lambda m: _DIACRITIC_TO_COMBINING.get(m.group(1), m.group(0)), md_text or ""
    )
    text = _COMBINING_SPACE_RE.sub(r"\1", text)
    return unicodedata.normalize("NFC", text)


def markdown_quality_scan(md_text: str) -> list[str]:
    """Return human-readable warnings about lost/odd characters in md_text."""
    text = md_text or ""
    warnings: list[str] = []
    n_replacement = len(_REPLACEMENT_CHAR_RE.findall(text))
    if n_replacement:
        warnings.append(
            f"{n_replacement} 个替换字符（�）——PDF 字库缺失的符号（如 ©）无法恢复，"
            "名称/符号可能需人工核对"
        )
    if text:
        nonprintable = sum(1 for c in text if _NONPRINTABLE_RE.match(c))
        if nonprintable / max(len(text), 1) > 0.01:
            warnings.append(
                f"{nonprintable} 个不可打印字符（比例 {nonprintable / len(text):.1%}）——"
                "文本编码或字体映射异常"
            )
    return warnings


def pdf_to_markdown(pdf_path: str | Path, write: bool = True) -> str | Path:
    """Convert a downloaded PDF to markdown alongside it.

    Returns the written .md path (write=True) or the markdown text.
    Raises a RuntimeError with an install hint when pymupdf4llm is missing.
    """
    md_text, _warnings = pdf_to_markdown_detailed(pdf_path, write=write)
    return md_text


def pdf_to_markdown_detailed(pdf_path: str | Path, write: bool = True) -> tuple[str | Path, list[str]]:
    """Convert a PDF to markdown; return (markdown, quality_warnings).

    ``markdown_warnings`` are non-fatal — the text is readable but some
    characters (lost font glyphs) may need spot-checking.
    """
    try:
        import pymupdf4llm
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "markdown 导出需要 pymupdf4llm：pip install pymupdf4llm"
        ) from exc

    src = Path(pdf_path)
    raw_md = pymupdf4llm.to_markdown(str(src))
    md_text = clean_markdown_text(raw_md)
    warnings = markdown_quality_scan(md_text)
    if not write:
        return md_text, warnings
    out = src.with_suffix(".md")
    out.write_text(md_text, encoding="utf-8")
    return out, warnings