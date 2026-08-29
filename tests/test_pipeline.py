"""Pipeline tests: queue schema, channel prediction, URL extraction, tables, SI links."""

from __future__ import annotations

from pathlib import Path

from scansci_pdf.pipeline import (
    QueueEntry,
    collect_failures,
    entries_from_table,
    extract_identifier,
    load_job,
    parse_queue,
    predict_channel,
    read_table,
    write_queue,
)
from scansci_pdf.supplementary import extract_supplementary_links


# --- channel prediction -------------------------------------------------------

def test_predict_channel_elsevier_prefix():
    assert predict_channel("10.1016/j.watres.2023.121036") == "elsevier"
    assert predict_channel("10.1038/nature12373") == "auto"
    assert predict_channel("2401.12345") == "auto"


# --- identifier extraction ----------------------------------------------------

def test_extract_identifier_from_doi_url():
    assert extract_identifier("https://doi.org/10.1016/j.watres.2023.121036") == "10.1016/j.watres.2023.121036"


def test_extract_identifier_bare_doi():
    assert extract_identifier("10.1038/nature12373") == "10.1038/nature12373"


def test_extract_identifier_trailing_author_glue():
    assert extract_identifier("10.1002/ird.2673Hamed") == "10.1002/ird.2673"


def test_extract_identifier_arxiv_url_and_id():
    assert extract_identifier("https://arxiv.org/abs/2401.12345") == "2401.12345"
    assert extract_identifier("arxiv:2401.12345") == "2401.12345"


def test_extract_identifier_none_for_prose():
    assert extract_identifier("this is not an identifier at all") is None
    assert extract_identifier("") is None


# --- queue parsing / writing --------------------------------------------------

def test_parse_queue_plain_list():
    entries = parse_queue("10.1038/nature12373\n\n# comment\n10.1016/j.watres.2023.121036\n")
    assert [e.identifier for e in entries] == ["10.1038/nature12373", "10.1016/j.watres.2023.121036"]
    assert entries[1].channel == "elsevier"  # predicted from prefix


def test_parse_queue_tsv_with_channel_and_oa_url():
    line = "10.1038/nature12373\toa\thttps://example.org/paper.pdf"
    e = parse_queue(line)[0]
    assert e.channel == "oa"
    assert e.oa_url == "https://example.org/paper.pdf"


def test_parse_queue_unresolved_line_is_kept():
    entries = parse_queue("this line is a vague citation\n10.1038/nature12373")
    assert entries[0].unresolved is True
    assert entries[1].identifier == "10.1038/nature12373"


def test_queue_round_trip(tmp_path: Path):
    entries = [
        QueueEntry(identifier="10.1016/j.watres.2023.121036", channel="elsevier", title="Water research"),
        QueueEntry(identifier="10.1038/nature12373", channel="auto"),
        QueueEntry(raw="vague line", unresolved=True),
    ]
    p = write_queue(entries, tmp_path / "job.queue")
    loaded = parse_queue(p.read_text(encoding="utf-8"))
    assert [e.identifier for e in loaded if e.identifier] == [
        "10.1016/j.watres.2023.121036",
        "10.1038/nature12373",
    ]
    assert loaded[0].channel == "elsevier"


# --- table inputs -------------------------------------------------------------

def test_entries_from_table_sniffs_doi_column(tmp_path: Path):
    csv_path = tmp_path / "list.csv"
    csv_path.write_text(
        "标题,作者,DOI链接\n"
        "Water research stuff, Someone, https://doi.org/10.1016/j.watres.2023.121036\n"
        "Another paper, Other, https://doi.org/10.1038/nature12373\n",
        encoding="utf-8",
    )
    rows = read_table(csv_path)
    assert len(rows) == 2
    entries = entries_from_table(rows)
    assert [e.identifier for e in entries] == ["10.1016/j.watres.2023.121036", "10.1038/nature12373"]
    assert entries[0].channel == "elsevier"


def test_load_job_csv(tmp_path: Path):
    csv_path = tmp_path / "job.csv"
    csv_path.write_text("doi\n10.1038/nature12373\nnot-a-doi-text\n", encoding="utf-8")
    entries = load_job(csv_path)
    assert sum(1 for e in entries if e.identifier) == 1
    assert sum(1 for e in entries if e.unresolved) == 1


# --- failures -----------------------------------------------------------------

def test_collect_failures_handles_both_result_shapes():
    racing = [{"success": True, "doi": "10.1/a"}, {"success": False, "doi": "10.2/b"}]
    cascade = [{"status": "success", "doi": "10.3/c"}, {"doi": "10.4/d", "error": "x"}]
    assert collect_failures(racing) == ["10.2/b"]
    assert collect_failures(cascade) == ["10.4/d"]


# --- supplementary link extraction --------------------------------------------

def test_extract_supplementary_links_filters_and_resolves():
    html = """
    <a href="/cms/attachment/12345/si1.pdf">SI 1</a>
    <a href="https://example.com/supplementary/data.xlsx">data</a>
    <a href="/about">about the journal</a>
    <a href="/cms/attachment/99999/main.pdf">main</a>
    <a href="https://media.springernature.com/original/springer-static/esm/art%3A10.1038%2Fs41586-021-03819-2/MediaObjects/41586_2021_3819_MOESM1_ESM.pdf">SI PDF</a>
    <a href="/articles/s41586-021-03819-2#MOESM1">anchor, not a file</a>
    """
    links = extract_supplementary_links(html, "https://www.sciencedirect.com/science/article/pii/S12345678")
    assert len(links) == 3
    assert links[0].startswith("https://www.sciencedirect.com/cms/")
    assert links[1] == "https://example.com/supplementary/data.xlsx"
    assert "MOESM1_ESM.pdf" in links[2]


def test_extract_supplementary_links_empty_html():
    assert extract_supplementary_links("", "https://example.com") == []
