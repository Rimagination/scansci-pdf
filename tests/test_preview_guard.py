"""Regression tests: Elsevier 1-page previews must never pass as full text."""

import os
from pathlib import Path
from unittest.mock import patch

import fitz
import pytest


def _make_pdf(pages: int, min_size: int = 0) -> bytes:
    """Build an in-memory PDF with `pages` pages, padded above min_size bytes.

    Padding uses an uncompressed embedded stream (incompressible random
    bytes), so size-based heuristics (>50KB / >100KB) are genuinely exercised.
    """
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Test document page {i + 1}")
    if min_size and len(doc.tobytes()) < min_size:
        blob = os.urandom(min_size)
        xref = doc.get_new_xref()
        doc.update_object(xref, f"<< /Type /EmbeddedFile /Length {len(blob)} >>")
        doc.update_stream(xref, blob, compress=False)
    data = doc.tobytes()
    if min_size:
        assert len(data) >= min_size, f"padding failed: {len(data)} < {min_size}"
    return data


class TestIsSuspiciousPdf:
    def test_large_single_page_is_suspicious(self, tmp_path: Path):
        """The core regression: a >100KB 1-page Elsevier preview used to pass
        because is_suspicious_pdf exempted large files outright."""
        p = tmp_path / "preview.pdf"
        p.write_bytes(_make_pdf(1, min_size=150_000))
        assert p.stat().st_size > 100_000
        from scansci_pdf.pdf_utils import is_suspicious_pdf
        assert is_suspicious_pdf(p) is True

    def test_large_multi_page_is_not_suspicious(self, tmp_path: Path):
        p = tmp_path / "full.pdf"
        p.write_bytes(_make_pdf(3, min_size=150_000))
        from scansci_pdf.pdf_utils import is_suspicious_pdf
        assert is_suspicious_pdf(p) is False

    def test_small_file_is_suspicious(self, tmp_path: Path):
        p = tmp_path / "cover.pdf"
        p.write_bytes(_make_pdf(1))
        from scansci_pdf.pdf_utils import is_suspicious_pdf
        assert is_suspicious_pdf(p) is True


class TestInstsciElsevierPreview:
    def test_try_elsevier_api_rejects_single_page_preview(self, tmp_path: Path):
        from scansci_pdf.institutional import instsci_bridge

        class _Resp:
            status_code = 200
            content = _make_pdf(1, min_size=150_000)

        out = tmp_path / "10.1016_j.fake.1.pdf"
        with patch("requests.get", return_value=_Resp()):
            result = instsci_bridge._try_elsevier_api(
                "10.1016/j.fake.1", out, {"elsevier_api_key": "k"}
            )
        assert result is None
        assert not out.exists(), "preview bytes must not be written to disk"

    def test_try_elsevier_api_accepts_full_text(self, tmp_path: Path):
        from scansci_pdf.institutional import instsci_bridge

        class _Resp:
            status_code = 200
            content = _make_pdf(5, min_size=150_000)

        out = tmp_path / "10.1016_j.fake.2.pdf"
        with patch("requests.get", return_value=_Resp()):
            result = instsci_bridge._try_elsevier_api(
                "10.1016/j.fake.2", out, {"elsevier_api_key": "k"}
            )
        assert result is not None and result["success"] is True
        assert result["source"] == "institutional:elsevier_api"

    def test_institutional_fetch_pdf_rejects_single_page(self):
        """The second institutional Elsevier PDF path must reject previews too."""
        from scansci_pdf.institutional.sources import elsevier_api as inst_api

        class _Resp:
            status_code = 200
            headers = {"content-type": "application/pdf"}
            content = _make_pdf(1, min_size=150_000)

        with patch("requests.Session.get", return_value=_Resp()):
            assert inst_api.fetch_pdf("10.1016/j.fake.3", "k") is None


class TestRacingElsevierPreview:
    def test_valid_pdf_bytes_rejects_single_page(self):
        from scansci_pdf.sources.elsevier_api import _valid_pdf_bytes

        assert _valid_pdf_bytes(_make_pdf(1, min_size=150_000), "t", reject_single_page=True) is False
        assert _valid_pdf_bytes(_make_pdf(4, min_size=150_000), "t", reject_single_page=True) is True
        assert _valid_pdf_bytes(_make_pdf(1, min_size=150_000), "t", reject_single_page=False) is True


if __name__ == "__main__":
    pytest.main([__file__])
