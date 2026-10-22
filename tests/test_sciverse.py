"""Sciverse API client: request shape, filters, errors, and search merging."""

from __future__ import annotations

import json

import pytest

from scansci_pdf.sources import sciverse as sciverse_mod
from scansci_pdf.sources.sciverse import (
    SciverseError,
    agentic_search,
    build_filters,
    content,
    is_configured,
    search_sciverse,
    to_search_results,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text
        self.content = content

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def _config(**overrides):
    cfg = {
        "sciverse_enabled": True,
        "sciverse_api_key": "test-token",
        "sciverse_base_url": "https://sciverse.test",
        "connect_timeout": 1,
        "read_timeout": 2,
        "sciverse_timeout_seconds": 45,
    }
    cfg.update(overrides)
    return cfg


def _hit(**overrides):
    hit = {
        "chunk_id": "c_1",
        "chunk": "Graphene cathodes improve cycle stability.",
        "doc_id": "d_abc",
        "title": "Cycle stability of graphene cathodes",
        "abstract": "An abstract",
        "score": 0.87,
        "offset": 18432,
        "page_no": 4,
        "author": ["Jane Doe", "John Smith"],
        "publication_venue_name_unified": "Nature",
        "publication_venue_type": "journal",
        "publication_published_date": "2023-06-15",
        "publication_published_year": 2023,
        "citation_count": 128,
        "primary_topic": "Energy Storage",
        "primary_topic_domain": "Physical Sciences",
    }
    hit.update(overrides)
    return hit


def test_is_configured_requires_key_and_enabled():
    assert is_configured(_config()) is True
    assert is_configured(_config(sciverse_api_key="")) is False
    assert is_configured(_config(sciverse_enabled=False)) is False


def test_agentic_search_builds_documented_request(monkeypatch):
    session = FakeSession(FakeResponse(payload={"hits": []}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)

    payload = agentic_search(
        "  graphene battery  ",
        config=_config(),
        top_k=500,
        sub_queries=9,
        filters={"lang": "en"},
    )
    assert payload == {"hits": []}
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == "https://sciverse.test/agentic-search"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["json"] == {
        "query": "graphene battery",
        "top_k": 100,       # clamped to the documented ceiling
        "sub_queries": 4,   # clamped to 0-4
        "filters": {"lang": "en"},
    }
    assert kwargs["timeout"] == (1, 45)


def test_agentic_search_rejects_empty_query():
    with pytest.raises(SciverseError) as err:
        agentic_search("   ", config=_config())
    assert err.value.code == "INVALID_REQUEST"


def test_build_filters_combines_fields():
    filters = build_filters(
        lang="en",
        year_from=2020,
        year_to=2025,
        venue="Nature",
        venue_type="journal",
        author="Jane Doe",
        topic=["Energy Storage", "Materials"],
        topic_domain="Physical Sciences",
        topic_logic="and",
        doc_ids=["d_1", " ", "d_2"],
        citation_min=10,
        citation_max=100,
        extra={"title": "Exact"},
    )
    assert filters == {
        "lang": "en",
        "publication_venue_name_unified": "Nature",
        "publication_venue_type": "journal",
        "publication_published_year": {"gte": 2020, "lte": 2025},
        "citation_count": {"gte": 10, "lte": 100},
        "author": ["Jane Doe"],
        "topics": {
            "logic": "and",
            "dimensions": {
                "primary_topic": ["Energy Storage", "Materials"],
                "primary_topic_domain": "Physical Sciences",
            },
        },
        "doc_id": ["d_1", "d_2"],
        "title": "Exact",
    }


def test_content_omits_limit_without_offset(monkeypatch):
    session = FakeSession(FakeResponse(payload={"text": "full", "more": False}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)

    payload = content("d_abc", config=_config())
    assert payload["text"] == "full"
    assert payload["chars_returned"] == 4  # live API may omit it; fallback fills it
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://sciverse.test/content"
    assert kwargs["params"] == {"doc_id": "d_abc"}

    content("d_abc", config=_config(), offset=0, limit=700)
    assert session.calls[1][2]["params"] == {"doc_id": "d_abc", "offset": 0, "limit": 700}


def test_content_rejects_negative_offset():
    with pytest.raises(SciverseError) as err:
        content("d_abc", config=_config(), offset=-1)
    assert err.value.code == "INVALID_REQUEST"


def test_error_is_structured_and_not_retried(monkeypatch):
    response = FakeResponse(
        401,
        {"error": {"code": "UNAUTHORIZED", "message": "bad token"}},
        text="bad token",
    )
    session = FakeSession(response)
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)

    with pytest.raises(SciverseError) as err:
        agentic_search("q", config=_config())
    assert err.value.status_code == 401
    assert err.value.code == "UNAUTHORIZED"
    assert err.value.retryable is False
    assert "sciverse_api_key" in err.value.action
    assert len(session.calls) == 1

    payload = err.value.to_dict()
    assert payload["success"] is False
    assert payload["error_code"] == "UNAUTHORIZED"


def test_5xx_is_retried(monkeypatch):
    session = FakeSession(
        FakeResponse(503, {"code": "UPSTREAM_UNAVAILABLE", "message": "busy"}, text="busy"),
        FakeResponse(payload={"hits": [{"doc_id": "d_1"}]}),
    )
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)
    monkeypatch.setattr(sciverse_mod.time, "sleep", lambda seconds: None)

    payload = agentic_search("q", config=_config())
    assert payload["hits"][0]["doc_id"] == "d_1"
    assert len(session.calls) == 2


def test_non_json_success_is_an_error(monkeypatch):
    session = FakeSession(FakeResponse(payload=ValueError("not json"), text="<html>"))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)

    with pytest.raises(SciverseError) as err:
        agentic_search("q", config=_config())
    assert "non-JSON" in str(err.value)


def test_to_search_results_groups_chunks_by_doc():
    payload = {
        "hits": [
            _hit(),
            _hit(chunk_id="c_2", chunk="Second passage", offset=20000, score=0.5),
            _hit(
                chunk_id="c_3",
                doc_id="d_other",
                title="Another paper",
                author="Solo Author",
                publication_published_year=None,
                publication_published_date="2021-01-02",
                citation_count=7,
            ),
        ]
    }
    results = to_search_results(payload)
    assert len(results) == 2

    first = results[0]
    assert first["doi"] == ""
    assert first["doc_id"] == "d_abc"
    assert first["title"] == "Cycle stability of graphene cathodes"
    assert first["authors"] == ["Jane Doe", "John Smith"]
    assert first["year"] == 2023
    assert first["cited_by_count"] == 128
    assert first["journal"] == "Nature"
    assert first["snippet"].startswith("Graphene cathodes")
    assert [e["chunk_id"] for e in first["evidence"]] == ["c_1", "c_2"]
    assert first["evidence"][0]["page_no"] == 4
    assert first["evidence"][0]["offset"] == 18432

    second = results[1]
    assert second["authors"] == ["Solo Author"]
    assert second["year"] == 2021


def test_to_search_results_honors_limit():
    payload = {"hits": [_hit(doc_id="d_1"), _hit(doc_id="d_2"), _hit(doc_id="d_3")]}
    assert len(to_search_results(payload, limit=2)) == 2


def test_search_sciverse_unconfigured_returns_empty():
    assert search_sciverse("q", config=_config(sciverse_api_key="")) == []


def test_search_sciverse_requests_wider_chunk_pool(monkeypatch):
    session = FakeSession(FakeResponse(payload={"hits": [_hit()]}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)
    cfg = _config(sciverse_sub_queries=2)

    results = search_sciverse("graphene", limit=5, year_from=2020, config=cfg)
    assert len(results) == 1
    body = session.calls[0][2]["json"]
    assert body["top_k"] == 15
    assert body["sub_queries"] == 2
    assert body["filters"] == {"publication_published_year": {"gte": 2020}}


def test_build_meta_filters_uses_prefixed_operators():
    from scansci_pdf.sources.sciverse import build_meta_filters

    filters = build_meta_filters(
        year_from=2020,
        year_to=2025,
        lang="en",
        venue="Nature",
        venue_type="journal",
        author="Jane Doe",
        topic="Energy Storage",
        topic_domain="Physical Sciences",
        doi="10.1/x",
        doc_ids=["d1"],
        citation_min=10,
        citation_max=100,
    )
    assert filters == [
        {"field": "publication_published_year", "operator": "FILTER_OP_GTE", "value": 2020},
        {"field": "publication_published_year", "operator": "FILTER_OP_LTE", "value": 2025},
        {"field": "citation_count", "operator": "FILTER_OP_GTE", "value": 10},
        {"field": "citation_count", "operator": "FILTER_OP_LTE", "value": 100},
        {"field": "language", "operator": "FILTER_OP_EQ", "value": "en"},
        {"field": "publication_venue_name_unified", "operator": "FILTER_OP_MATCH_PHRASE", "value": "Nature"},
        {"field": "publication_venue_type", "operator": "FILTER_OP_EQ", "value": "journal"},
        {"field": "author", "operator": "FILTER_OP_MATCH", "value": "Jane Doe"},
        {"field": "primary_topic.display_name", "operator": "FILTER_OP_EQ", "value": "Energy Storage"},
        {"field": "primary_topic.domain.display_name", "operator": "FILTER_OP_EQ", "value": "Physical Sciences"},
        {"field": "doi", "operator": "FILTER_OP_EQ", "value": "10.1/x"},
        {"field": "doc_id", "operator": "FILTER_OP_IN", "value": ["d1"]},
    ]


def test_build_meta_sort_normalizes_orders():
    from scansci_pdf.sources.sciverse import build_meta_sort

    assert build_meta_sort("citation_count:asc") == [
        {"field": "citation_count", "order": "SORT_ORDER_ASC"}
    ]
    assert build_meta_sort([{"field": "publication_published_year", "order": "asc"}]) == [
        {"field": "publication_published_year", "order": "SORT_ORDER_ASC"}
    ]
    assert build_meta_sort(None) is None


def test_meta_search_builds_body_and_cursor(monkeypatch):
    from scansci_pdf.sources.sciverse import meta_search

    session = FakeSession(FakeResponse(payload={"results": []}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)

    filters = [{"field": "doi", "operator": "FILTER_OP_EQ", "value": "10.1/x"}]
    meta_search(
        "graphene",
        config=_config(),
        filters=filters,
        sort=[{"field": "citation_count", "order": "SORT_ORDER_DESC"}],
        fields=["title", "doi"],
        page=2,
        page_size=50,
        freshness_boost="mild",
    )
    body = session.calls[0][2]["json"]
    assert body == {
        "collection": "papers",
        "query": "graphene",
        "filters": filters,
        "sort": [{"field": "citation_count", "order": "SORT_ORDER_DESC"}],
        "fields": ["title", "doi"],
        "page": 2,
        "page_size": 50,
        "freshness_boost": "MILD",
    }

    meta_search("graphene", config=_config(), filters=filters, cursor="opaque")
    cursor_body = session.calls[1][2]["json"]
    assert cursor_body["cursor"] == "opaque"
    assert "page" not in cursor_body and "page_size" not in cursor_body


def test_meta_search_marks_capped_total(monkeypatch):
    from scansci_pdf.sources.sciverse import meta_search

    session = FakeSession(FakeResponse(payload={"results": [], "total_count": 10000}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)
    payload = meta_search("q", config=_config())
    assert payload["total_count_capped"] is True

    session = FakeSession(FakeResponse(payload={"results": [], "total_count": 9999}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)
    payload = meta_search("q", config=_config())
    assert "total_count_capped" not in payload


def test_enrich_records_with_meta_fills_doi(monkeypatch):
    from scansci_pdf.sources.sciverse import enrich_records_with_meta

    records = [{
        "doc_id": "d_1",
        "title": "agentic title",
        "doi": "",
        "year": "",
        "cited_by_count": 0,
        "journal": "",
        "source": "sciverse",
    }]
    monkeypatch.setattr(
        sciverse_mod,
        "_metadata_by_doc_id",
        lambda ids, cfg: {
            "d_1": {
                "doi": "10.1/x",
                "unique_id": "paper:10.1/x",
                "year": 2022,
                "cited_by_count": 11,
                "journal": "Nature",
            }
        },
    )
    out = enrich_records_with_meta(records, _config())
    assert out[0]["doi"] == "10.1/x"
    assert out[0]["unique_id"] == "paper:10.1/x"
    assert out[0]["year"] == 2022
    assert out[0]["cited_by_count"] == 11
    assert out[0]["journal"] == "Nature"


def test_enrich_hits_with_metadata_fills_raw_fields(monkeypatch):
    from scansci_pdf.sources.sciverse import enrich_hits_with_metadata

    hits = [{"doc_id": "d_1", "title": "t"}]
    monkeypatch.setattr(
        sciverse_mod,
        "_metadata_by_doc_id",
        lambda ids, cfg: {
            "d_1": {
                "doi": "10.1/x",
                "unique_id": "paper:10.1/x",
                "year": 2022,
                "cited_by_count": 11,
                "journal": "Nature",
            }
        },
    )
    out = enrich_hits_with_metadata(hits, _config())
    assert out[0]["doi"] == "10.1/x"
    assert out[0]["unique_id"] == "paper:10.1/x"
    assert out[0]["publication_published_year"] == 2022
    assert out[0]["citation_count"] == 11
    assert out[0]["publication_venue_name_unified"] == "Nature"


def test_search_sciverse_enriches_doi_when_requested(monkeypatch):
    from scansci_pdf.sources.sciverse import search_sciverse

    session = FakeSession(FakeResponse(payload={"hits": [_hit()]}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)
    monkeypatch.setattr(
        sciverse_mod,
        "_metadata_by_doc_id",
        lambda ids, cfg: {"d_abc": {"doi": "10.1/x", "unique_id": "paper:10.1/x"}},
    )
    results = search_sciverse("q", limit=1, config=_config(), enrich_dois=True)
    assert results[0]["doi"] == "10.1/x"
    assert results[0]["unique_id"] == "paper:10.1/x"


def test_meta_search_requires_query_or_filters():
    from scansci_pdf.sources.sciverse import meta_search

    with pytest.raises(SciverseError) as err:
        meta_search("", config=_config())
    assert err.value.code == "INVALID_REQUEST"


def test_meta_search_results_maps_metadata():
    from scansci_pdf.sources.sciverse import meta_search_results

    payload = {
        "results": [{
            "title": "A paper",
            "doi": "10.1/x",
            "author": [{"name": "Jane Doe"}, {"name": "John Smith"}],
            "publication_published_year": 2024,
            "citation_count": 12,
            "abstract": "abstract",
            "access_is_oa": True,
            "access_oa_url": "https://example.test/a.pdf",
            "publication_venue_name_unified": "Nature",
            "publication_venue_type": "journal",
            "primary_topic": {
                "display_name": "AI",
                "domain": {"display_name": "Computer Science"},
            },
            "doc_id": "d_1",
            "unique_id": "paper:10.1/x",
            "language": "en",
            "influential_citation_count": 3,
            "fwci": 1.5,
        }]
    }
    results = meta_search_results(payload)
    assert results == [{
        "title": "A paper",
        "doi": "10.1/x",
        "url": "https://doi.org/10.1/x",
        "authors": ["Jane Doe", "John Smith"],
        "year": 2024,
        "cited_by_count": 12,
        "abstract": "abstract",
        "is_oa": True,
        "oa_url": "https://example.test/a.pdf",
        "source": "sciverse-meta",
        "doc_id": "d_1",
        "unique_id": "paper:10.1/x",
        "journal": "Nature",
        "venue_type": "journal",
        "primary_topic": "AI",
        "primary_topic_domain": "Computer Science",
        "language": "en",
        "influential_citation_count": 3,
        "fwci": 1.5,
    }]


def test_meta_search_results_coerces_float_year_and_counts():
    from scansci_pdf.sources.sciverse import meta_search_results

    payload = {
        "results": [{
            "title": "A paper",
            "publication_published_year": 2022.0,
            "citation_count": 11.0,
            "influential_citation_count": 3.0,
        }]
    }
    row = meta_search_results(payload)[0]
    assert row["year"] == 2022
    assert row["cited_by_count"] == 11
    assert row["influential_citation_count"] == 3


def test_to_search_results_coerces_float_year_and_counts():
    from scansci_pdf.sources.sciverse import to_search_results

    payload = {
        "hits": [_hit(
            publication_published_year=2022.0,
            citation_count=11.0,
        )]
    }
    row = to_search_results(payload)[0]
    assert row["year"] == 2022
    assert row["cited_by_count"] == 11


def test_meta_search_papers_builds_year_filters(monkeypatch):
    from scansci_pdf.sources.sciverse import meta_search_papers

    session = FakeSession(FakeResponse(payload={"results": []}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)

    meta_search_papers("q", limit=5, year_from=2020, config=_config())
    body = session.calls[0][2]["json"]
    assert body["page_size"] == 5
    assert body["filters"] == [
        {"field": "publication_published_year", "operator": "FILTER_OP_GTE", "value": 2020}
    ]


def test_paper_relations_builds_body_and_validates(monkeypatch):
    from scansci_pdf.sources.sciverse import paper_relations

    session = FakeSession(FakeResponse(payload={"items": [], "total_count": 0}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)

    payload = paper_relations("paper:10.1/x", relation="citations", page=2, page_size=10, config=_config())
    assert payload["total_count"] == 0
    body = session.calls[0][2]["json"]
    assert body == {
        "unique_id": "paper:10.1/x",
        "relation": "CITATIONS",
        "page": 2,
        "page_size": 10,
    }

    paper_relations("10.1234/x", config=_config())
    assert session.calls[1][2]["json"]["unique_id"] == "paper:10.1234/x"

    with pytest.raises(SciverseError):
        paper_relations("paper:10.1/x", relation="NOPE", config=_config())


def test_meta_catalog_params(monkeypatch):
    from scansci_pdf.sources.sciverse import meta_catalog

    session = FakeSession(FakeResponse(payload={"fields": []}))
    monkeypatch.setattr(sciverse_mod, "_get_session", lambda cfg: session)

    assert meta_catalog(collection="authors", include_sample_values=True, config=_config()) == {"fields": []}
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://sciverse.test/meta-catalog"
    assert kwargs["params"] == {"collection": "authors", "include_sample_values": "true"}


def test_resource_rejects_unsafe_names():
    from scansci_pdf.sources.sciverse import resource

    for bad in ("", "../x", "/abs", "http://x", "a\\b"):
        with pytest.raises(SciverseError) as err:
            resource(bad, config=_config())
        assert err.value.code == "INVALID_REQUEST"


def test_resource_rejects_html_error_page(monkeypatch):
    from scansci_pdf.sources.sciverse import resource

    monkeypatch.setattr(
        sciverse_mod,
        "_request",
        lambda *args, **kwargs: FakeResponse(
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=b"<html>error</html>",
        ),
    )
    with pytest.raises(SciverseError) as err:
        resource("f3.png", config=_config())
    assert err.value.code == "NOT_FOUND"


def test_save_resource_writes_binary(monkeypatch, tmp_path):
    from scansci_pdf.sources.sciverse import save_resource

    monkeypatch.setattr(
        sciverse_mod,
        "_request",
        lambda *args, **kwargs: FakeResponse(
            headers={
                "Content-Type": "image/png",
                "Content-Disposition": 'attachment; filename="f3.png"',
            },
            content=b"PNG",
        ),
    )
    saved = save_resource("dt=x/p/f3.png", tmp_path / "out", config=_config())
    target = tmp_path / "out" / "f3.png"
    assert target.read_bytes() == b"PNG"
    assert saved["path"] == str(target)
    assert saved["content_type"] == "image/png"
    assert saved["size"] == 3


def test_mcp_sciverse_meta_search_dispatches(monkeypatch):
    from scansci_pdf import server
    from scansci_pdf.sources import sciverse as mod

    monkeypatch.setattr(server, "load_config", lambda: _config())
    monkeypatch.setattr(
        mod,
        "meta_search",
        lambda *args, **kwargs: {"results": [{"title": "X"}], "total_count": 1},
    )
    out = json.loads(server.scansci_pdf_sciverse(action="meta_search", query="q"))
    assert out["success"] is True
    assert out["source"] == "sciverse-meta"
    assert out["count"] == 1


def test_mcp_sciverse_relations_and_catalog_dispatch(monkeypatch):
    from scansci_pdf import server
    from scansci_pdf.sources import sciverse as mod

    monkeypatch.setattr(server, "load_config", lambda: _config())
    monkeypatch.setattr(
        mod,
        "paper_relations",
        lambda *args, **kwargs: {"items": [{"title": "T"}], "total_count": 1},
    )
    out = json.loads(server.scansci_pdf_sciverse(action="relations", unique_id="paper:1"))
    assert out["success"] is True and out["relation"] == "REFERENCES"

    monkeypatch.setattr(mod, "meta_catalog", lambda *args, **kwargs: {"fields": [{"name": "doi"}]})
    out = json.loads(server.scansci_pdf_sciverse(action="catalog"))
    assert out["success"] is True and out["fields"][0]["name"] == "doi"


def test_merge_preserves_sciverse_fields_on_title_duplicate():
    from scansci_pdf.search import _merge_search_results

    base = [{
        "title": "石墨烯电池循环稳定性",
        "doi": "",
        "authors": ["张三"],
        "year": 2022,
        "cited_by_count": 3,
        "source": "ncpssd",
    }]
    extra = [{
        "title": "石墨烯电池循环稳定性",
        "doi": "10.9999/test",
        "authors": ["张三"],
        "year": 2022,
        "cited_by_count": 9,
        "source": "sciverse",
        "doc_id": "d_x",
        "evidence": [{"chunk_id": "c_x", "chunk": "evidence", "offset": 5}],
    }]

    merged = _merge_search_results(base, extra)
    assert len(merged) == 1
    record = merged[0]
    assert record["doi"] == "10.9999/test"
    assert record["doc_id"] == "d_x"
    assert record["evidence"][0]["chunk_id"] == "c_x"
    assert record["cited_by_count"] == 9
    assert set(record["source"].split("+")) == {"ncpssd", "sciverse"}


def test_merge_uses_doc_id_when_titles_differ():
    from scansci_pdf.search import _merge_search_results

    base = [{
        "title": "agentic title",
        "doc_id": "d_x",
        "source": "sciverse",
        "evidence": [{"chunk_id": "c_x", "chunk": "evidence"}],
    }]
    extra = [{
        "title": "metadata title",
        "doc_id": "d_x",
        "doi": "10.1/x",
        "unique_id": "paper:10.1/x",
        "source": "sciverse-meta",
    }]
    merged = _merge_search_results(base, extra)
    assert len(merged) == 1
    assert merged[0]["doi"] == "10.1/x"
    assert merged[0]["unique_id"] == "paper:10.1/x"
    assert merged[0]["evidence"][0]["chunk_id"] == "c_x"


def test_merge_does_not_collapse_different_dois():
    from scansci_pdf.search import _merge_search_results

    base = [{"title": "Same title", "doi": "10.1/a", "source": "openalex"}]
    extra = [{"title": "Same title", "doi": "10.1/b", "source": "sciverse"}]
    assert len(_merge_search_results(base, extra)) == 2


def test_key_preview_masks_tokens():
    from scansci_pdf.sources.sciverse import key_preview

    assert key_preview("") == ""
    assert key_preview("short") == "***"
    assert key_preview("abcdefghijklmnop") == "abcdef...mnop"


def test_validate_key_ok(monkeypatch):
    from scansci_pdf.sources.sciverse import validate_key

    monkeypatch.setattr(
        sciverse_mod,
        "meta_catalog",
        lambda **kwargs: {"fields": [{"name": "doi"}], "contract_version": "2026-01"},
    )
    out = validate_key(_config())
    assert out["status"] == "ok"
    assert out["fields"] == 1
    assert out["contract_version"] == "2026-01"


def test_validate_key_maps_unauthorized(monkeypatch):
    from scansci_pdf.sources.sciverse import validate_key

    def boom(**kwargs):
        raise SciverseError("bad token", status_code=401, code="UNAUTHORIZED")

    monkeypatch.setattr(sciverse_mod, "meta_catalog", boom)
    out = validate_key(_config())
    assert out["status"] == "invalid"
    assert out["status_code"] == 401


def test_validate_key_unconfigured():
    from scansci_pdf.sources.sciverse import validate_key

    out = validate_key(_config(sciverse_api_key=""))
    assert out["status"] == "not_configured"


def test_sciverse_setup_unconfigured_opens_console(monkeypatch):
    from scansci_pdf import server

    opened = []
    monkeypatch.setattr(server, "load_config", lambda: _config(sciverse_api_key=""))
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    out = json.loads(server.scansci_pdf_sciverse_setup())
    assert out["status"] == "not_configured"
    assert out["browser_opened"] is True
    assert opened == ["https://sciverse.space/tokens"]
    assert "scansci-pdf sciverse-setup" in out["setup_command"]


def test_sciverse_setup_saves_and_validates(monkeypatch):
    from scansci_pdf import server
    from scansci_pdf.sources import sciverse as mod

    cfg = _config(sciverse_api_key="")
    monkeypatch.setattr(server, "load_config", lambda: dict(cfg))

    def fake_update(key, value):
        cfg[key] = value

    monkeypatch.setattr(server, "update_config", fake_update)
    monkeypatch.setattr(
        mod, "validate_key", lambda config=None: {"status": "ok", "fields": 42}
    )

    out = json.loads(
        server.scansci_pdf_sciverse_setup(token="new-token-123456", test=True)
    )
    assert out["saved"] is True
    assert out["status"] == "configured"
    assert out["test"] == "ok"
    assert cfg["sciverse_api_key"] == "new-token-123456"


def test_sciverse_setup_invalid_token(monkeypatch):
    from scansci_pdf import server
    from scansci_pdf.sources import sciverse as mod

    cfg = _config(sciverse_api_key="")
    monkeypatch.setattr(server, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(server, "update_config", lambda key, value: cfg.update({key: value}))
    monkeypatch.setattr(
        mod,
        "validate_key",
        lambda config=None: {"status": "invalid", "message": "bad token"},
    )

    out = json.loads(server.scansci_pdf_sciverse_setup(token="bad-token-123456"))
    assert out["test"] == "invalid"
    assert "验证失败" in out["message"]


def test_config_reports_sciverse_unset_with_setup_hint(monkeypatch):
    from scansci_pdf import server

    monkeypatch.setattr(server, "load_config", lambda: _config(sciverse_api_key=""))
    monkeypatch.setattr(
        server,
        "scansci_pdf_config_get",
        lambda: json.dumps({"sciverse_api_key": ""}),
    )
    out = json.loads(server.scansci_pdf_config(key="sciverse_api_key"))
    assert out["configured"] is False
    assert out["value"] == "(unset)"
    assert "scansci_pdf_sciverse_setup" in out["setup"]


def test_config_reports_sciverse_configured(monkeypatch):
    from scansci_pdf import server

    monkeypatch.setattr(server, "load_config", lambda: _config())
    monkeypatch.setattr(
        server,
        "scansci_pdf_config_get",
        lambda: json.dumps({"sciverse_api_key": "***"}),
    )
    out = json.loads(server.scansci_pdf_config(key="sciverse_api_key"))
    assert out["configured"] is True
    assert out["value"] == "***"
    assert "setup" not in out


def test_search_returns_sciverse_config_hint(monkeypatch):
    from scansci_pdf import server

    monkeypatch.setattr(server, "load_config", lambda: _config(sciverse_api_key=""))
    monkeypatch.setattr(server, "search_papers", lambda *args, **kwargs: [])

    out = json.loads(server.scansci_pdf_search(query="q"))
    assert out["results"] == []
    assert out["config_hint"]["service"] == "sciverse"
    assert out["config_hint"]["status"] == "not_configured"


def test_mcp_sciverse_requires_key(monkeypatch):
    from scansci_pdf import server

    monkeypatch.setattr(server, "load_config", lambda: _config(sciverse_api_key=""))
    out = json.loads(server.scansci_pdf_sciverse(action="search", query="q"))
    assert out["success"] is False
    assert "scansci_pdf_config" in out["action"]


def test_mcp_sciverse_unknown_action_lists_allowed():
    from scansci_pdf import server

    out = json.loads(server.scansci_pdf_sciverse(action="nope"))
    assert out["allowed"] == [
        "search", "content", "meta_search", "relations", "resource", "catalog",
    ]
