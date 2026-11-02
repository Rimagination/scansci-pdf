from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from scansci_pdf import browser_backend, publisher_batch, publisher_profiles, session_broker, sources
from scansci_pdf.identifiers import safe_filename
from scansci_pdf.institutional import instsci_bridge


@pytest.fixture(autouse=True)
def _no_external_state(monkeypatch):
    monkeypatch.setattr(sources, "_save_progress", lambda *args: None)
    monkeypatch.setattr(sources, "_browser_semaphore", threading.Semaphore(2))
    monkeypatch.setattr(browser_backend, "is_available", lambda: True)
    monkeypatch.setattr(session_broker, "broker_is_running", lambda _key: False)


def _profile(name):
    return SimpleNamespace(name=name)


def _profile_inference(monkeypatch, by_doi=None, by_url=None):
    by_doi = by_doi or {}
    by_url = by_url or {}
    monkeypatch.setattr(publisher_profiles, "infer_publisher_profile", lambda doi: by_doi.get(doi))
    monkeypatch.setattr(
        publisher_profiles, "infer_publisher_profile_from_url", lambda url: by_url.get(url)
    )


def _write_manifest(run_dir, records, *, success=False):
    from pathlib import Path

    run_dir = Path(run_dir)
    complete = run_dir / "complete"
    complete.mkdir(parents=True, exist_ok=True)
    entries = []
    for record in records:
        if success:
            pdf_path = run_dir / f"{safe_filename(record.doi)}.pdf"
            pdf_path.write_bytes(b"%PDF-" + b"x" * 6000)
            entries.append({
                "doi": record.doi, "status": "success", "pdf_path": str(pdf_path)
            })
        else:
            entries.append({"doi": record.doi, "status": "failed", "reason": "test failure"})
    (complete / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")


class _DirectDownloader:
    calls = []
    active = 0
    peak = 0
    key_active = {}
    key_peak = {}
    lock = threading.Lock()
    delay = 0.1

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.active = 0
        cls.peak = 0
        cls.key_active = {}
        cls.key_peak = {}
        cls.lock = threading.Lock()

    def __init__(self, *, profile, **kwargs):
        self.profile = profile

    def run_records(self, records, run_dir, *, retry_failed, concurrency, report_progress):
        key = session_broker.broker_key(self.profile.name)
        with self.lock:
            type(self).calls.append({
                "key": key,
                "dois": [record.doi for record in records],
                "concurrency": concurrency,
                "report_progress": report_progress,
            })
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
            type(self).key_active[key] = type(self).key_active.get(key, 0) + 1
            type(self).key_peak[key] = max(
                type(self).key_peak.get(key, 0), type(self).key_active[key]
            )
        try:
            time.sleep(self.delay)
            _write_manifest(run_dir, records)
        finally:
            with self.lock:
                type(self).active -= 1
                type(self).key_active[key] -= 1


def test_direct_groups_share_one_session_per_key_and_overlap_different_keys(monkeypatch, tmp_path):
    acs = _profile("ACS")
    elsevier = _profile("Elsevier")
    by_doi = {
        "10.1/a1": acs,
        "10.1/a2": acs,
        "10.2/e1": elsevier,
        "10.2/e2": elsevier,
    }
    _profile_inference(monkeypatch, by_doi)
    _DirectDownloader.reset()
    monkeypatch.setattr(publisher_batch, "PublisherBatchDownloader", _DirectDownloader)
    monkeypatch.setattr(
        sources, "download", lambda *args, **kwargs: pytest.fail("manifest rows must not fall back")
    )
    results = {doi: {"success": False} for doi in by_doi}

    sources._batch_institutional_phase(
        list(by_doi), tmp_path,
        {"institutional_workers": 2, "max_browser_workers": 2},
        "batch", results,
    )

    assert len(_DirectDownloader.calls) == 2
    assert _DirectDownloader.peak == 2
    assert set(_DirectDownloader.key_peak.values()) == {1}
    assert all(call["concurrency"] == 1 for call in _DirectDownloader.calls)
    assert all(call["report_progress"] is False for call in _DirectDownloader.calls)
    assert sorted(len(call["dois"]) for call in _DirectDownloader.calls) == [2, 2]


def test_ungrouped_dois_are_resolved_and_submitted_as_whole_broker_jobs(monkeypatch, tmp_path):
    acs = _profile("ACS")
    elsevier = _profile("Elsevier")
    dois = ["10.9/a1", "10.9/a2", "10.9/e1"]
    urls = {doi: f"https://publisher.test/{doi}" for doi in dois}
    _profile_inference(
        monkeypatch,
        by_url={"https://publisher.test/10.9/a1": acs,
                "https://publisher.test/10.9/a2": acs,
                "https://publisher.test/10.9/e1": elsevier},
    )
    monkeypatch.setattr(instsci_bridge, "_resolve_doi", lambda doi: urls[doi])
    monkeypatch.setattr(session_broker, "broker_is_running", lambda _key: True)
    monkeypatch.setattr(
        publisher_batch, "PublisherBatchDownloader",
        lambda **kwargs: pytest.fail("an active broker must not spawn a direct downloader"),
    )
    jobs = []

    def submit(**kwargs):
        jobs.append(kwargs)
        records = [SimpleNamespace(**record) for record in kwargs["records"]]
        _write_manifest(kwargs["output_dir"], records, success=True)
        return {"status": "success"}

    monkeypatch.setattr(session_broker, "submit_broker_job", submit)
    results = {doi: {"success": False} for doi in dois}

    sources._batch_institutional_phase(
        dois, tmp_path, {"institutional_workers": 2, "max_browser_workers": 2},
        "batch", results,
    )

    assert sorted((job["publisher"], len(job["records"])) for job in jobs) == [
        ("acs", 2), ("elsevier", 1)
    ]
    assert all(results[doi]["success"] for doi in dois)
    assert {results[doi]["source"].split(":")[1] for doi in dois} == {"broker"}


def test_broker_failure_uses_one_direct_session_and_reports_browser(monkeypatch, tmp_path):
    acs = _profile("ACS")
    dois = ["10.1/a1", "10.1/a2"]
    _profile_inference(monkeypatch, {doi: acs for doi in dois})
    monkeypatch.setattr(session_broker, "broker_is_running", lambda _key: True)
    monkeypatch.setattr(
        session_broker, "submit_broker_job",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broker stopped")),
    )
    _DirectDownloader.reset()

    class SuccessfulDirect(_DirectDownloader):
        def run_records(self, records, run_dir, **kwargs):
            _write_manifest(run_dir, records, success=True)

    monkeypatch.setattr(publisher_batch, "PublisherBatchDownloader", SuccessfulDirect)
    results = {doi: {"success": False} for doi in dois}

    sources._batch_institutional_phase(dois, tmp_path, {}, "batch", results)

    assert all(results[doi]["success"] for doi in dois)
    assert {results[doi]["source"].split(":")[1] for doi in dois} == {"browser"}


def test_broker_failures_retry_only_the_failed_subset_directly(monkeypatch, tmp_path):
    acs = _profile("ACS")
    dois = ["10.1/a1", "10.1/a2"]
    _profile_inference(monkeypatch, {doi: acs for doi in dois})
    monkeypatch.setattr(session_broker, "broker_is_running", lambda _key: True)

    def submit(**kwargs):
        from pathlib import Path

        records = [SimpleNamespace(**record) for record in kwargs["records"]]
        _write_manifest(kwargs["output_dir"], records, success=True)
        manifest_path = Path(kwargs["output_dir"]) / "complete" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest[1]["status"] = "failed"
        manifest[1]["reason"] = "transient"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(session_broker, "submit_broker_job", submit)
    direct_calls = []

    class PartialDirect:
        def __init__(self, **_kwargs):
            pass

        def run_records(self, records, run_dir, **_kwargs):
            direct_calls.append([record.doi for record in records])
            _write_manifest(run_dir, records, success=True)

    monkeypatch.setattr(publisher_batch, "PublisherBatchDownloader", PartialDirect)
    results = {doi: {"success": False} for doi in dois}

    sources._batch_institutional_phase(dois, tmp_path, {}, "batch", results)

    assert direct_calls == [["10.1/a2"]]
    assert results["10.1/a1"]["source"].split(":")[1] == "broker"
    assert results["10.1/a2"]["source"].split(":")[1] == "browser"


def test_local_cache_short_circuits_resolution_and_browser_work(monkeypatch, tmp_path):
    doi = "10.9/cached"
    _profile_inference(monkeypatch)
    cached = tmp_path / f"{safe_filename(doi)}.pdf"
    cached.write_bytes(b"%PDF-" + b"x" * 6000)
    monkeypatch.setattr(
        instsci_bridge, "_resolve_doi",
        lambda _doi: pytest.fail("a cached DOI must not be resolved again"),
    )
    monkeypatch.setattr(
        sources, "download",
        lambda *args, **kwargs: pytest.fail("a cached DOI must not be downloaded again"),
    )
    results = {doi: {"success": False}}

    sources._batch_institutional_phase([doi], tmp_path, {}, "batch", results)

    assert results[doi]["success"]
    assert results[doi]["source"] == "local_cache"


def test_unresolved_dois_keep_the_per_doi_fallback(monkeypatch, tmp_path):
    dois = ["10.9/x1", "10.9/x2"]
    _profile_inference(monkeypatch)
    monkeypatch.setattr(instsci_bridge, "_resolve_doi", lambda _doi: None)
    attempted = []

    def fallback(doi, *_args, **_kwargs):
        attempted.append(doi)
        return {"doi": doi, "identifier": doi, "success": False, "error": "no access"}

    monkeypatch.setattr(sources, "download", fallback)
    results = {doi: {"success": False} for doi in dois}

    sources._batch_institutional_phase(dois, tmp_path, {}, "batch", results)

    assert attempted == dois
    assert not any(result["success"] for result in results.values())


def test_run_records_progress_opt_out_preserves_the_default(monkeypatch, tmp_path):
    from scansci_pdf import progress_reporter

    events = []
    monkeypatch.setattr(progress_reporter, "start_task", lambda *args, **kwargs: events.append("start"))
    monkeypatch.setattr(progress_reporter, "finish", lambda: events.append("finish"))
    downloader = publisher_batch.PublisherBatchDownloader(
        config={}, profile=_profile("ACS"), require_interactive_browser=True,
    )
    monkeypatch.setattr(downloader, "_run_once", lambda *args, **kwargs: [])
    monkeypatch.setattr(downloader, "_count_results", lambda _results: {})
    monkeypatch.setattr(downloader, "_write_complete_artifacts", lambda *args, **kwargs: {})

    downloader.run_records([], tmp_path / "quiet", report_progress=False)
    assert events == []
    assert downloader._progress_active is False

    downloader.run_records([], tmp_path / "default")
    assert events == ["start", "finish"]
    assert downloader._progress_active is False
