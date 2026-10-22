"""`rename_pdf` must not mistake one paper for another.

The duplicate test used to be "the two files are within 1 KB of each other in
size", and on that basis it deleted the file that had just been downloaded and
returned the pre-existing one — silently, with no warning. Two papers from the
same journal template can easily be that close in length (#70).
"""

from __future__ import annotations

from pathlib import Path

from scansci_pdf import rename as rename_mod

METADATA = {"authors": ["Jane Doe"], "year": 2024, "title": "Some Paper"}


def _target_path(directory: Path) -> Path:
    return directory / f"{rename_mod.generate_filename(METADATA)}.pdf"


def test_different_pdfs_of_similar_size_are_not_treated_as_duplicates(tmp_path):
    downloaded = tmp_path / "download.pdf"
    downloaded_bytes = b"%PDF-1.7\n" + b"A" * 5000 + b"\n%%EOF"
    downloaded.write_bytes(downloaded_bytes)

    existing = _target_path(tmp_path)
    existing_bytes = b"%PDF-1.7\n" + b"B" * 5001 + b"\n%%EOF"  # 1 byte longer
    existing.write_bytes(existing_bytes)

    result = rename_mod.rename_pdf(downloaded, METADATA)

    # The fresh download must survive under a free name, and the unrelated
    # file must be left exactly as it was.
    assert result is not None
    assert result != existing
    assert result.read_bytes() == downloaded_bytes
    assert existing.read_bytes() == existing_bytes
    assert not downloaded.exists()


def test_identical_pdfs_are_still_deduplicated(tmp_path):
    """The reuse path is wanted when the bytes really do match."""
    payload = b"%PDF-1.7\n" + b"C" * 5000 + b"\n%%EOF"

    existing = _target_path(tmp_path)
    existing.write_bytes(payload)
    downloaded = tmp_path / "download.pdf"
    downloaded.write_bytes(payload)

    result = rename_mod.rename_pdf(downloaded, METADATA)

    assert result == existing
    assert not downloaded.exists()
    assert existing.read_bytes() == payload


def test_same_size_but_different_content_is_not_deduplicated(tmp_path):
    """Size equality is only the cheap pre-filter; the hash decides."""
    downloaded = tmp_path / "download.pdf"
    downloaded.write_bytes(b"%PDF-1.7\n" + b"D" * 5000 + b"\n%%EOF")

    existing = _target_path(tmp_path)
    existing_bytes = b"%PDF-1.7\n" + b"E" * 5000 + b"\n%%EOF"  # equal size
    existing.write_bytes(existing_bytes)

    result = rename_mod.rename_pdf(downloaded, METADATA)

    assert result is not None and result != existing
    assert existing.read_bytes() == existing_bytes
