"""Tests for the ScanSci Find <-> scansci-pdf bridge: download result
write-back, preprint fallback retries, and fallback-map building."""

import json

import pytest

from scansci_pdf.discovery import build_preprint_fallbacks
from scansci_pdf.sources import _apply_preprint_fallbacks, _write_download_results


class TestDownloadResultsWriteBack:
    def test_write_download_results_persists_entries(self, tmp_path):
        results = [
            {"success": True, "identifier": "10.1000/x", "doi": "10.1000/x", "source": "Sci-Hub(se)", "file": "x.pdf", "cached": False},
            {"success": False, "identifier": "10.1000/y", "doi": "10.1000/y", "error_type": "paywall", "reason": "login required"},
        ]

        _write_download_results(results, tmp_path)

        payload = json.loads((tmp_path / "download_results.json").read_text(encoding="utf-8"))
        assert payload["total"] == 2
        assert payload["succeeded"] == 1
        assert payload["entries"][0]["source"] == "Sci-Hub(se)"
        assert payload["entries"][1]["error_type"] == "paywall"

    def test_write_download_results_handles_missing_output_dir(self, tmp_path):
        _write_download_results([], tmp_path / "nested" / "out")
        assert (tmp_path / "nested" / "out" / "download_results.json").exists()


class TestPreprintFallbacks:
    def test_failed_doi_retries_with_preprint_arxiv_id(self, monkeypatch, tmp_path):
        calls = []
        results = {"10.1000/x": {"success": False, "identifier": "10.1000/x", "reason": "failed"}}
        progress_saved = {}

        def fake_download(identifier, output_dir=None, **kwargs):
            calls.append((identifier, kwargs))
            return {"success": True, "identifier": "2401.00001", "doi": "", "file": "preprint.pdf", "source": "arXiv"}

        def fake_save(batch_id, ident, result):
            progress_saved[ident] = result

        monkeypatch.setattr("scansci_pdf.sources.download", fake_download)
        monkeypatch.setattr("scansci_pdf.sources._save_progress", fake_save)

        _apply_preprint_fallbacks(
            {"10.1000/x": ["2401.00001"]}, ["10.1000/x"], results, "batch-1",
            output_dir=tmp_path, scihub_enabled=True, use_tor=False, use_vpnsci=True,
        )

        assert calls == [("2401.00001", {"scihub_enabled": True, "use_tor": False, "use_vpnsci": False, "_institutional": False})]
        assert results["10.1000/x"]["success"] is True
        assert results["10.1000/x"]["preprint_fallback"] == "2401.00001"
        assert progress_saved["10.1000/x"]["preprint_fallback"] == "2401.00001"

    def test_successful_dois_are_not_retried(self, monkeypatch):
        def fake_download(identifier, **kwargs):
            raise AssertionError("fallback must not run for successful identifiers")

        monkeypatch.setattr("scansci_pdf.sources.download", fake_download)

        results = {"10.1000/x": {"success": True, "identifier": "10.1000/x"}}
        _apply_preprint_fallbacks({"10.1000/x": ["2401.00001"]}, ["10.1000/x"], results, "batch-1", output_dir=None, scihub_enabled=True, use_tor=False, use_vpnsci=True)
        assert results["10.1000/x"]["success"] is True

    def test_fallback_failure_keeps_original_result(self, monkeypatch):
        monkeypatch.setattr(
            "scansci_pdf.sources.download",
            lambda identifier, output_dir=None, **kwargs: {"success": False, "identifier": identifier, "reason": "not found"},
        )

        results = {"10.1000/x": {"success": False, "identifier": "10.1000/x", "reason": "failed"}}
        _apply_preprint_fallbacks({"10.1000/x": ["2401.00001"]}, ["10.1000/x"], results, "batch-1", output_dir=None, scihub_enabled=True, use_tor=False, use_vpnsci=True)
        assert results["10.1000/x"].get("preprint_fallback") is None


class TestBuildPreprintFallbacks:
    def test_builds_map_from_download_queue(self, tmp_path):
        (tmp_path / "download_queue.json").write_text(
            json.dumps(
                [
                    {"identifier": "10.1000/x", "preprint_identifiers": [{"type": "arxiv", "value": "2401.00001", "url": ""}]},
                    {"identifier": "10.1000/y"},
                    {"identifier": "10.1000/z", "preprint_identifiers": [{"type": "arxiv", "value": "2401.00002", "url": ""}]},
                ]
            ),
            encoding="utf-8",
        )

        fallbacks = build_preprint_fallbacks(tmp_path)

        assert fallbacks == {"10.1000/x": ["2401.00001"], "10.1000/z": ["2401.00002"]}

    def test_returns_empty_map_without_queue(self, tmp_path):
        assert build_preprint_fallbacks(tmp_path) == {}
