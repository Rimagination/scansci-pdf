"""Markdown export: PDF -> agent-ready .md via pymupdf4llm."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from scansci_pdf.md_export import pdf_to_markdown


def _tiny_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello scansci markdown export test 2026")
    doc.save(str(p))
    doc.close()
    return p


def test_pdf_to_markdown_writes_sibling_md(tmp_path: Path):
    pdf = _tiny_pdf(tmp_path)
    out = pdf_to_markdown(pdf)
    assert Path(out).exists()
    assert Path(out).suffix == ".md"
    assert "scansci markdown export test" in Path(out).read_text(encoding="utf-8")


def test_pdf_to_markdown_text_only_mode(tmp_path: Path):
    pdf = _tiny_pdf(tmp_path)
    text = pdf_to_markdown(pdf, write=False)
    assert isinstance(text, str)
    assert "scansci markdown export test" in text


def test_clean_markdown_text_folds_sup_caret_diacritics():
    from scansci_pdf.md_export import clean_markdown_text

    cleaned = clean_markdown_text("A<sup>ˇ</sup>Z c<sup>ˇ</sup>")
    assert "<sup>" not in cleaned
    # NFC folds A + U+030C into the precomposed Ǎ (U+01CD)
    assert "\u01cd" in cleaned   # Ǎ
    assert "\u010d" in cleaned   # č


def test_clean_markdown_text_keeps_lone_sup_caret():
    from scansci_pdf.md_export import clean_markdown_text

    cleaned = clean_markdown_text("plain <sup>ˇ</sup>")
    assert "<sup>" not in cleaned
    assert "\u030c" in cleaned   # combining caron survives as a diacritic


def test_markdown_quality_scan_flags_replacement_chars():
    from scansci_pdf.md_export import markdown_quality_scan

    warnings = markdown_quality_scan("good text \ufffd broken")
    assert any("替换字符" in w for w in warnings)
    assert markdown_quality_scan("clean text") == []
