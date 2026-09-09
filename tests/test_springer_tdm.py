"""Springer TDM lane: gate, statuses, artifact, and fast-lane wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from scansci_pdf.sources import springer_tdm
from scansci_pdf.sources.springer_tdm import try_springer_tdm


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _mock_session(monkeypatch, resp):
    class _S:
        def get(self, *a, **k):
            return resp

    monkeypatch.setattr(springer_tdm, "_session", lambda config: _S())


DOI = "10.1007/s00208-025-03286-4"
FULL = '<response><article><body><p>full text</p></body></article></response>'
EMPTY = '<response><query>...</query></response>'


class TestTrySpringerTdm:
    def test_no_key_is_silent_skip(self, tmp_path: Path):
        assert try_springer_tdm(DOI, tmp_path / "a.pdf", {}) is None

    def test_non_springer_doi_skips(self, tmp_path: Path):
        assert try_springer_tdm(
            "10.1016/j.envres.2024.118134", tmp_path / "a.pdf",
            {"springer_api_key": "k"}) is None

    def test_entitled_fulltext_saves_xml(self, tmp_path: Path, monkeypatch):
        _mock_session(monkeypatch, _Resp(200, FULL))
        r = try_springer_tdm(DOI, tmp_path / "out.pdf",
                             {"springer_api_key": "k"})
        assert r is not None and r["success"] is True
        assert r["source"] == "SpringerTDM"
        xml = Path(r["file"])
        assert xml.suffix == ".xml" and "<body" in xml.read_text(encoding="utf-8")

    def test_invalid_key_surfaces_config_needed(self, tmp_path: Path, monkeypatch):
        _mock_session(monkeypatch, _Resp(401, "denied"))
        r = try_springer_tdm(DOI, tmp_path / "out.pdf",
                             {"springer_api_key": "bad"})
        assert r is not None and not r["success"]
        assert r["error_type"] == "config_needed"

    def test_not_entitled_is_silent(self, tmp_path: Path, monkeypatch):
        _mock_session(monkeypatch, _Resp(200, EMPTY))
        assert try_springer_tdm(DOI, tmp_path / "out.pdf",
                                {"springer_api_key": "k"}) is None

    def test_403_maps_to_not_entitled(self, tmp_path: Path, monkeypatch):
        _mock_session(monkeypatch, _Resp(403, ""))
        assert try_springer_tdm(DOI, tmp_path / "out.pdf",
                                {"springer_api_key": "k"}) is None


class TestValidation:
    def test_validate_entitled(self, monkeypatch):
        _mock_session(monkeypatch, _Resp(200, FULL))
        v = springer_tdm.validate_springer_key("k", {})
        assert v["status"] == "entitled"

    def test_validate_invalid_key(self, monkeypatch):
        _mock_session(monkeypatch, _Resp(401, ""))
        v = springer_tdm.validate_springer_key("k", {})
        assert v["status"] == "invalid_key"

    def test_validate_not_entitled(self, monkeypatch):
        _mock_session(monkeypatch, _Resp(200, EMPTY))
        v = springer_tdm.validate_springer_key("k", {})
        assert v["status"] == "not_entitled"

    def test_validate_no_key(self):
        assert springer_tdm.validate_springer_key("", {})["status"] == "no_key"


class TestWiring:
    def test_springer_publisher_maps_tdm_first(self):
        from scansci_pdf.sources.publishers import PUBLISHER_TOOL_MAP

        assert PUBLISHER_TOOL_MAP["Springer"][0] == "SpringerTDM"

    def test_fast_lane_hits_springer(self, tmp_path: Path, monkeypatch):
        from scansci_pdf import pipeline
        from scansci_pdf.pipeline import QueueEntry

        called = []
        monkeypatch.setattr(
            "scansci_pdf.sources.springer_tdm.try_springer_tdm",
            lambda doi, out_path, config: called.append(doi) or {
                "success": True, "file": str(out_path.with_suffix(".xml")),
                "source": "SpringerTDM",
            })
        entries = [QueueEntry(identifier=DOI)]
        results = pipeline._run_fast_lane(
            entries, tmp_path, {"springer_api_key": "k"})
        assert called == [DOI]
        assert results[0]["success"] and results[0]["source"] == "springer_tdm"

    def test_fast_lane_skips_springer_without_key(self, tmp_path: Path, monkeypatch):
        from scansci_pdf import pipeline
        from scansci_pdf.pipeline import QueueEntry

        called = []
        monkeypatch.setattr(
            "scansci_pdf.sources.springer_tdm.try_springer_tdm",
            lambda doi, out_path, config: called.append(doi) or None)
        entries = [QueueEntry(identifier=DOI)]
        pipeline._run_fast_lane(entries, tmp_path, {})
        assert called == []


if __name__ == "__main__":
    pytest.main([__file__])
