"""Offline tests for the Chinese DOI / NCPSSD / CN-database lanes."""

from __future__ import annotations

from pathlib import Path

import pytest

from scansci_pdf.caj import caj_hints, guess_format, is_caj_bytes, is_caj_file
from scansci_pdf.chinese import (
    DoiResolution,
    is_chinese_doi,
    normalize_cn_text,
    parse_chinese_doi,
    parse_chndoi_handler,
    parse_cnki_detail,
    parse_journal_meta,
    parse_wanfang_detail,
    title_similarity,
)
from scansci_pdf.identifiers import normalize_doi, validate_doi
from scansci_pdf.rename import generate_filename
from scansci_pdf.sources import _finalize_result_file
from scansci_pdf.sources.cn_databases import _cnki_download, detect_cn_database
from scansci_pdf.sources.cn_journal import find_journal_pdf_links
from scansci_pdf.sources.carsi import detect_publisher
from scansci_pdf.sources import ncpssd


# ---------------------------------------------------------------------------
# DOI / text normalisation
# ---------------------------------------------------------------------------

def test_normalize_doi_fullwidth_and_chinese_punctuation():
    raw = "  doi：１０．３９６９／j.issn.1000-0135.2023.01.001。 "
    assert normalize_doi(raw) == "10.3969/j.issn.1000-0135.2023.01.001"


def test_is_chinese_doi_patterns():
    assert is_chinese_doi("10.3969/j.issn.1000-0135.2023.01.001")
    assert is_chinese_doi("10.16235/j.cnki.33-1005/c.2025.03.002")
    assert is_chinese_doi("10.13266/j.issn.0252-3116.2023.01.001")
    assert is_chinese_doi("10.11897/SP.J.1016.2023.00001")
    assert not is_chinese_doi("10.1038/nature12373")
    assert not is_chinese_doi("10.1016/j.cell.2020.01.001")


def test_parse_chinese_doi_istic_fields():
    parsed = parse_chinese_doi("10.3969/j.issn.1000-0135.2023.01.001")
    assert parsed == {
        "kind": "istic",
        "issn": "1000-0135",
        "year": "2023",
        "issue": "01",
        "seq": "001",
    }
    assert parse_chinese_doi("10.16235/j.cnki.33-1005/c.2025.03.002")["kind"] == "cnki"
    assert parse_chinese_doi("10.1038/nature12373") is None


def test_normalize_cn_text_strips_highlight_and_fullwidth_brackets():
    raw = '<font color="red"><b>论人工智能</b></font>（综述）：进展'
    assert normalize_cn_text(raw) == "论人工智能(综述):进展"


def test_title_similarity_tolerates_punctuation_and_font_tags():
    left = '<font color="red">论人工智能报关</font>'
    right = "论人工智能报关（综述）"
    assert title_similarity(left, right) >= 0.9
    assert title_similarity("论人工智能报关", "完全不同的标题") < 0.5


def test_validate_doi_accepts_chinese_without_head_request(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("validate_doi must not HEAD a Chinese DOI")

    import scansci_pdf.identifiers as identifiers

    monkeypatch.setattr("requests.head", _boom)
    valid, message = validate_doi("10.3969/j.issn.1000-0135.2023.01.001")
    assert valid is True
    assert "Chinese DOI" in message


# ---------------------------------------------------------------------------
# chndoi / CNKI / Wanfang response parsing
# ---------------------------------------------------------------------------

CHNDOI_HTML = """
<div class="fbBd"><table>
<tr><td><label>题名：</label>如何理解人工智能——人工智能的哲学考察</td></tr>
<tr><td><label>作者：</label>杨国荣;</td></tr>
<tr><td><label>出版机构：</label>同方知网（北京）技术有限公司</td></tr>
</table>
<ul>
<li><a href="https://link.cnki.net/doi/10.16235/j.cnki.33-1005/c.2025.03.002">境内</a></li>
<li><a href="https://link.oversea.cnki.net/doi/10.16235/j.cnki.33-1005/c.2025.03.002">境外</a></li>
</ul></div>
"""


def test_parse_chndoi_handler_extracts_metadata_and_cnki_urls():
    parsed = parse_chndoi_handler(CHNDOI_HTML)
    assert parsed["title"] == "如何理解人工智能-人工智能的哲学考察"
    assert parsed["authors"] == ["杨国荣"]
    assert parsed["publisher"] == "同方知网(北京)技术有限公司"
    assert any("link.cnki.net" in url for url in parsed["urls"])


CNKI_HTML = """
<html><head><title>如何理解人工智能-中国知网</title>
<meta name="citation_author" content="杨国荣">
</head><body>
<script>var dbcode="CJFQ"; var tablename="CJFDLAST2025"; var filename="ZJXK202503001";</script>
<a onclick="WriteKrsDownLog()" target="_blank" id="cajDown" name="cajDown"
   href="https://bar.cnki.net/bar/download/order?id=caj123"></a>
<a onclick="WriteKrsDownLog()" target="_blank" id="pdfDown" name="pdfDown"
   href="https://bar.cnki.net/bar/download/order?id=pdf456"></a>
</body></html>
"""


def test_parse_cnki_detail_extracts_ids_and_download_links():
    parsed = parse_cnki_detail(CNKI_HTML)
    assert parsed["filename"] == "ZJXK202503001"
    assert parsed["dbcode"] == "CJFQ"
    assert parsed["tablename"] == "CJFDLAST2025"
    assert parsed["pdf_url"].endswith("id=pdf456")
    assert parsed["caj_url"].endswith("id=caj123")
    assert parsed["authors"] == ["杨国荣"]


def _proto_field(number: int, data: bytes) -> bytes:
    def varint(value: int) -> bytes:
        out = bytearray()
        while True:
            chunk = value & 0x7F
            value >>= 7
            if value:
                out.append(chunk | 0x80)
            else:
                out.append(chunk)
                return bytes(out)

    return varint((number << 3) | 2) + varint(len(data)) + data


def test_parse_wanfang_detail_extracts_title_authors_and_ids():
    article = b"".join([
        _proto_field(1, b"wlgcygl202402008"),
        _proto_field(2, "论人工智能报关".encode("utf-8")),
        _proto_field(2, b"Research on AI Customs Declaration"),
        _proto_field(3, "乐烨".encode("utf-8")),
        _proto_field(3, "陈亚杰".encode("utf-8")),
        _proto_field(24, "物流工程与管理".encode("utf-8")),
        _proto_field(41, b"10.3969/j.issn.1674-4993.2024.02.008"),
        _proto_field(43, b"https://www.ncpssd.cn/Literature/articleinfo?id=WLGCYGL2024002008"),
    ])
    payload = _proto_field(1, _proto_field(2, _proto_field(103, article)))
    frame = b"\x00" + len(payload).to_bytes(4, "big") + payload
    parsed = parse_wanfang_detail(frame)
    assert parsed["file_id"] == "wlgcygl202402008"
    assert parsed["title"] == "论人工智能报关"
    assert parsed["authors"] == ["乐烨", "陈亚杰"]
    assert parsed["journal"] == "物流工程与管理"
    assert parsed["ncpssd_id"] == "WLGCYGL2024002008"


# ---------------------------------------------------------------------------
# NCPSSD
# ---------------------------------------------------------------------------

NCPSSD_ROW = {
    "id": "WLGCYGL2024002008",
    "title": "论人工智能报关",
    "doi": "10.3969/j.issn.1674-4993.2024.02.008",
    "cbw_name": "物流工程与管理",
    "years": "2024",
    "creator": "乐烨[1];陈亚杰[2]",
    "pdfurl": "https://ftprp.ncpssd.cn/pdf/getComm/npssd-prp/pdf?url=x.pdf",
}


def test_search_ncpssd_by_title_matches_normalised_title(monkeypatch):
    monkeypatch.setattr(ncpssd, "search_ncpssd", lambda *a, **k: [NCPSSD_ROW])
    row = ncpssd.search_ncpssd_by_title("论人工智能报关（综述）")
    assert row is NCPSSD_ROW


class _FakePdfResponse:
    status_code = 200
    headers = {"content-type": "application/pdf"}

    def __init__(self, body: bytes):
        self._body = body

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield self._body


class _FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


def test_download_ncpssd_row_writes_valid_pdf(monkeypatch, tmp_path):
    body = b"%PDF-1.7\n" + (b"x" * 2000) + b"\n%%EOF"
    monkeypatch.setattr(ncpssd, "_session", lambda config: _FakeSession(_FakePdfResponse(body)))
    output = tmp_path / "paper.pdf"
    result = ncpssd.download_ncpssd_row(NCPSSD_ROW, "10.3969/x", output, {})
    assert result and result["success"]
    assert result["source"] == "NCPSSD"
    assert output.read_bytes() == body


def test_row_to_crossref_cleans_creator_markers():
    message = ncpssd.row_to_crossref(NCPSSD_ROW, "10.3969/x")
    assert message["title"] == ["论人工智能报关"]
    assert [a["family"] for a in message["author"]] == ["乐烨", "陈亚杰"]
    assert message["container-title"] == ["物流工程与管理"]
    assert message["published-print"]["date-parts"] == [[2024]]


# ---------------------------------------------------------------------------
# Journal site / CAJ / rename / routing
# ---------------------------------------------------------------------------

JOURNAL_HTML = """
<html><head>
<meta name="citation_pdf_url" content="/CN/PDF/10.13266/j.issn.0252-3116.2023.01.001">
</head><body>
<a href="/CN/article/downloadArticleFile.do?attachType=PDF&id=123">PDF下载</a>
<a href="/CN/article/downloadArticleFile.do?attachType=PDF&id=456">全文下载</a>
<a href="/supplementary/fig1.pdf">图1</a>
</body></html>
"""


def test_find_journal_pdf_links_prefers_meta_and_magtech():
    links = find_journal_pdf_links(JOURNAL_HTML, "http://www.lis.ac.cn/CN/10.13266/x")
    assert links[0].endswith("/CN/PDF/10.13266/j.issn.0252-3116.2023.01.001")
    assert any("attachType=PDF" in link for link in links)
    assert not any("fig1.pdf" in link for link in links)


JOURNAL_META_HTML = """
<html><head>
<meta name="citation_title" content="贯彻落实二十大精神，开创文献情报工作的高质量发展道路"/>
<meta name="citation_author" content="刘细文"/>
<meta name="citation_journal_title" content="图书情报工作"/>
<meta name="citation_publication_date" content="2023/01/05"/>
<meta name="citation_volume" content="67"/>
<meta name="citation_issue" content="1"/>
<meta name="citation_firstpage" content="4"/>
<meta name="citation_lastpage" content="8"/>
<meta name="citation_doi" content="10.13266/j.issn.0252-3116.2023.01.001"/>
</head></html>
"""


def test_parse_journal_meta_builds_crossref_shape():
    message = parse_journal_meta(JOURNAL_META_HTML)
    assert message["title"] == ["贯彻落实二十大精神,开创文献情报工作的高质量发展道路"]
    assert [a["family"] for a in message["author"]] == ["刘细文"]
    assert message["container-title"] == ["图书情报工作"]
    assert message["published-print"] == {"date-parts": [[2023]]}
    assert (message["volume"], message["issue"], message["page"]) == ("67", "1", "4-8")


def test_caj_detection_and_guidance(tmp_path):
    caj = tmp_path / "paper.caj"
    caj.write_bytes(b"CAJ\x00" + b"x" * 2000)
    assert is_caj_file(caj)
    assert is_caj_bytes(b"CAJ\x00")
    assert is_caj_bytes(b"KDH ")
    assert guess_format(b"CAJ\x00", "application/octet-stream") == "caj"
    hints = caj_hints(caj)
    assert hints["format"] == "caj"
    assert "CAJViewer" in " ".join(hints["how_to_open"])


def test_generate_filename_keeps_chinese_title_and_author():
    metadata = {
        "title": ["论人工智能报关（综述）"],
        "author": [{"family": "陈亚杰"}],
        "published-print": {"date-parts": [[2024]]},
    }
    assert generate_filename(metadata) == "陈亚杰2024_人工智能报关综述"


def test_finalize_result_file_keeps_caj_extension(tmp_path):
    source = tmp_path / "paper_CNKI.caj"
    source.write_bytes(b"CAJ\x00" + b"x" * 2000)
    result = {"file": str(source)}
    final = _finalize_result_file(result, tmp_path / "paper.pdf")
    assert Path(final["file"]).suffix == ".caj"
    assert Path(final["file"]).exists()


class _StreamResponse:
    def __init__(self, body: bytes, content_type: str):
        self.status_code = 200
        self.headers = {"content-type": content_type}
        self.url = "https://bar.cnki.net/bar/download/order?id=1"
        self._body = body

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield self._body


class _StreamSession:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


def test_cnki_download_keeps_caj_and_detects_login(tmp_path):
    caj = b"CAJ\x00" + (b"x" * 3000)
    session = _StreamSession(_StreamResponse(caj, "application/octet-stream"))
    outcome = _cnki_download(session, "https://bar.cnki.net/1", "https://link.cnki.net/x",
                             tmp_path / "paper.pdf", {})
    assert outcome[0] == "caj"
    assert outcome[1].suffix == ".caj"
    assert outcome[1].read_bytes() == caj

    login = _StreamSession(_StreamResponse(b"<html>\xe8\xaf\xb7\xe7\x99\xbb\xe5\xbd\x95</html>",
                                           "text/html"))
    assert _cnki_download(login, "https://bar.cnki.net/1", "https://link.cnki.net/x",
                          tmp_path / "paper2.pdf", {}) == "login"


def test_detect_cn_database_from_doi_shape():
    assert detect_cn_database("10.16235/j.cnki.33-1005/c.2025.03.002") == "cnki"
    assert detect_cn_database("10.3969/j.issn.1674-4993.2024.02.008") == "wanfang"
    assert detect_cn_database("10.1038/nature12373") == ""


def test_cn_database_falls_back_to_vip_when_wanfang_is_paywalled(monkeypatch):
    from scansci_pdf.sources import cn_databases

    resolution = DoiResolution(doi="10.3969/x", title="论人工智能报关")
    monkeypatch.setattr(cn_databases, "resolve_chinese_doi", lambda *a, **k: resolution)
    monkeypatch.setattr(
        cn_databases, "try_wanfang",
        lambda *a, **k: {"success": False, "action": "login_required"},
    )
    monkeypatch.setattr(
        cn_databases, "try_cqvip",
        lambda *a, **k: {"success": True, "source": "VIP"},
    )
    result = cn_databases.try_cn_database(
        "10.3969/j.issn.1674-4993.2024.02.008", Path("x.pdf"),
        {"cn_databases_enabled": True, "cn_vip_fallback": True},
    )
    assert result["success"] is True
    assert result["source"] == "VIP"


def test_cn_database_vip_fallback_can_be_disabled(monkeypatch):
    from scansci_pdf.sources import cn_databases

    resolution = DoiResolution(doi="10.3969/x", title="论人工智能报关")
    monkeypatch.setattr(cn_databases, "resolve_chinese_doi", lambda *a, **k: resolution)
    monkeypatch.setattr(
        cn_databases, "try_wanfang",
        lambda *a, **k: {"success": False, "action": "login_required"},
    )
    called = {"vip": False}
    monkeypatch.setattr(
        cn_databases, "try_cqvip",
        lambda *a, **k: called.__setitem__("vip", True),
    )
    result = cn_databases.try_cn_database(
        "10.3969/j.issn.1674-4993.2024.02.008", Path("x.pdf"),
        {"cn_databases_enabled": True, "cn_vip_fallback": False},
    )
    assert result["action"] == "login_required"
    assert called["vip"] is False


def test_carsi_config_ships_cn_databases():
    assert detect_publisher("https://www.cnki.net/") == "cnki"
    assert detect_publisher("https://d.wanfangdata.com.cn/periodical/x") == "wanfang"
    assert detect_publisher("https://qikan.cqvip.com/Qikan/Article/Detail?id=1") == "cqvip"


def test_chinese_resolution_metadata_shape():
    resolution = DoiResolution(
        doi="10.3969/x",
        title="论人工智能报关",
        authors=["乐烨", "陈亚杰"],
        publisher="万方数据",
    )
    metadata = resolution.as_metadata()
    assert metadata["title"] == ["论人工智能报关"]
    assert [a["family"] for a in metadata["author"]] == ["乐烨", "陈亚杰"]
