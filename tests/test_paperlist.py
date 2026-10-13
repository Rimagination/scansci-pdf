"""A plain DOI list is the primary documented input for a reading list.

`PaperEntry.title` had no default while being the first field, so the DOI-only
branch — `PaperEntry(doi=..., raw=...)` — raised `TypeError: __init__() missing
1 required positional argument: 'title'` before it could produce anything (#70).
"""

from __future__ import annotations

from scansci_pdf.paperlist import PaperEntry, _parse_doi_list, parse_paper_list

DOIS = ["10.1016/j.scitotenv.2021.149946", "10.1088/1748-9326/11/7/074016"]


def test_doi_only_reading_list_does_not_require_title(tmp_path):
    listing = tmp_path / "dois.txt"
    listing.write_text("\n".join(DOIS) + "\n", encoding="utf-8")

    entries = parse_paper_list(listing)

    assert [entry.doi for entry in entries] == DOIS
    assert [entry.raw for entry in entries] == DOIS
    assert all(entry.title == "" for entry in entries)


def test_doi_list_parser_accepts_commas_and_semicolons():
    entries = _parse_doi_list(f"{DOIS[0]}, {DOIS[1]}; not-a-doi")

    assert [entry.doi for entry in entries] == DOIS


def test_entry_can_be_built_from_a_doi_alone():
    entry = PaperEntry(doi=DOIS[0], raw=DOIS[0])

    assert entry.doi == DOIS[0]
    assert entry.title == ""
    assert entry.authors == []
