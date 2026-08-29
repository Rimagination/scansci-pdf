"""OpenAIRE source: URL extraction from nested payloads + XML fallback."""

from __future__ import annotations

from scansci_pdf.sources.openaire import extract_openaire_fulltext_urls, _extract_from_xml


def test_extracts_nested_webresource_urls():
    payload = {
        "response": {
            "results": {
                "result": [
                    {
                        "metadata": {"title": "X"},
                        "webresource": [{"url": "https://repo.example.org/123/article.pdf"}],
                    },
                    {
                        "children": {"url": "https://repo.example.org/456/landing"},
                    },
                ]
            }
        }
    }
    urls = extract_openaire_fulltext_urls(payload)
    assert urls == [
        "https://repo.example.org/123/article.pdf",
        "https://repo.example.org/456/landing",
    ]


def test_pdf_urls_sort_first():
    payload = {"a": {"url": "https://x.org/landing"}, "b": {"url": "https://x.org/paper.pdf"}}
    urls = extract_openaire_fulltext_urls(payload)
    assert urls[0] == "https://x.org/paper.pdf"


def test_dedupes_urls():
    assert extract_openaire_fulltext_urls({"u": {"url": "https://a/1.pdf"}, "v": {"url": "https://a/1.pdf"}}) == [
        "https://a/1.pdf"
    ]


def test_xml_fallback_extracts_webresources():
    xml = "<result><webresource><url>https://repo.example.org/a.pdf</url></webresource></result>"
    assert _extract_from_xml(xml) == ["https://repo.example.org/a.pdf"]


def test_non_string_and_missing_url_ignored():
    assert extract_openaire_fulltext_urls({"url": 123, "x": {"url": None}}) == []
