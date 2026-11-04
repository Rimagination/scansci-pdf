from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from scansci_pdf import browser_backend, publisher_batch, publisher_profiles, session_broker, sources
from scansci_pdf.identifiers import safe_filename
from scansci_pdf.institutional import instsci_bridge


_REAL_SAVE_PROGRESS = sources._save_progress


@pytest.fixture(autouse=True)
def _no_external_state(monkeypatch):
    monkeypatch.setattr(sources, "_save_progress", lambda *args: None)
    monkeypatch.setattr(sources, "_browser_semaphore", threading.Semaphore(2))
    monkeypatch.setattr(browser_backend, "is_available", lambda: True)
    monkeypatch.setattr(session_broker, "broker_is_running", lambda _key: False)


def _profile(name):
    return publisher_profiles.get_publisher_profile(name)


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
        key = publisher_profiles.publisher_profile_key(self.profile)
        with self.lock:
            type(self).calls.append({
                "key": key,
                "dois": [record.doi for record in records],
                "run_dir": str(run_dir),
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
    broker_states = iter([True, False])
    monkeypatch.setattr(session_broker, "broker_is_running", lambda _key: next(broker_states, False))
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
        return {}

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


@pytest.mark.parametrize("key,profile", list(publisher_profiles.PUBLISHER_PROFILES.items()))
def test_broker_key_is_the_canonical_registry_key(key, profile):
    assert publisher_profiles.publisher_profile_key(profile) == key


def test_same_key_is_serialized_across_batches_and_run_dirs_are_unique(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    acs = _profile("ACS")
    dois = ["10.1/a1", "10.1/a2"]
    _profile_inference(monkeypatch, {doi: acs for doi in dois})
    _DirectDownloader.reset()
    monkeypatch.setattr(publisher_batch, "PublisherBatchDownloader", _DirectDownloader)

    def run(_):
        results = {doi: {"success": False} for doi in dois}
        sources._batch_institutional_phase(
            dois, tmp_path,
            {"institutional_workers": 2, "max_browser_workers": 2},
            "batch", results,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))

    assert len(_DirectDownloader.calls) == 2
    assert _DirectDownloader.peak == 1
    assert len({call["run_dir"] for call in _DirectDownloader.calls}) == 2


def test_broker_timeout_never_overlaps_a_direct_or_per_doi_fallback(monkeypatch, tmp_path):
    acs = _profile("ACS")
    doi = "10.1/a1"
    _profile_inference(monkeypatch, {doi: acs})
    monkeypatch.setattr(session_broker, "broker_is_running", lambda _key: True)
    monkeypatch.setattr(
        session_broker, "submit_broker_job",
        lambda **kwargs: (_ for _ in ()).throw(TimeoutError("late job")),
    )
    monkeypatch.setattr(
        publisher_batch, "PublisherBatchDownloader",
        lambda **kwargs: pytest.fail("a timed-out broker job may still be active"),
    )
    monkeypatch.setattr(
        sources, "download",
        lambda *args, **kwargs: pytest.fail("a timed-out broker job must not fall back"),
    )
    results = {doi: {"success": False}}

    sources._batch_institutional_phase([doi], tmp_path, {}, "batch", results)

    assert not results[doi]["success"]
    assert "suppressed" in results[doi]["reason"]


def test_broker_discovery_error_still_uses_direct_fallback(monkeypatch, tmp_path):
    acs = _profile("ACS")
    doi = "10.1/a1"
    _profile_inference(monkeypatch, {doi: acs})
    monkeypatch.setattr(
        session_broker, "broker_is_running",
        lambda _key: (_ for _ in ()).throw(ValueError("bad pid")),
    )
    _DirectDownloader.reset()
    monkeypatch.setattr(publisher_batch, "PublisherBatchDownloader", _DirectDownloader)

    sources._batch_institutional_phase([doi], tmp_path, {}, "batch", {doi: {"success": False}})

    assert len(_DirectDownloader.calls) == 1


def test_bad_manifest_fields_reach_the_per_doi_fallback(monkeypatch, tmp_path):
    acs = _profile("ACS")
    doi = "10.1/a1"
    _profile_inference(monkeypatch, {doi: acs})

    class BadManifest:
        def __init__(self, **_kwargs):
            pass

        def run_records(self, records, run_dir, **_kwargs):
            complete = run_dir / "complete"
            complete.mkdir(parents=True, exist_ok=True)
            (complete / "manifest.json").write_text(json.dumps([
                {"doi": doi, "status": "success", "pdf_path": 1}
            ]), encoding="utf-8")

    monkeypatch.setattr(publisher_batch, "PublisherBatchDownloader", BadManifest)
    attempted = []

    def fallback(fallback_doi, *_args, **_kwargs):
        attempted.append(fallback_doi)
        return {"doi": fallback_doi, "identifier": fallback_doi, "success": False}

    monkeypatch.setattr(sources, "download", fallback)

    sources._batch_institutional_phase([doi], tmp_path, {}, "batch", {doi: {"success": False}})

    assert attempted == [doi]


def test_legacy_display_name_broker_state_remains_compatible(monkeypatch, tmp_path):
    profile = _profile("Springer Nature")
    doi = "10.1/s1"
    _profile_inference(monkeypatch, {doi: profile})
    monkeypatch.setattr(
        session_broker, "broker_is_running", lambda key: key == "springer-nature"
    )
    jobs = []

    def submit(**kwargs):
        jobs.append(kwargs["publisher"])
        records = [SimpleNamespace(**record) for record in kwargs["records"]]
        _write_manifest(kwargs["output_dir"], records, success=True)
        return {}

    monkeypatch.setattr(session_broker, "submit_broker_job", submit)
    results = {doi: {"success": False}}

    sources._batch_institutional_phase([doi], tmp_path, {}, "batch", results)

    assert jobs == ["springer-nature"]
    assert results[doi]["source"].split(":")[1] == "broker"


def test_unresolved_fallback_preserves_explicit_authorization(monkeypatch, tmp_path):
    doi = "10.9/x1"
    _profile_inference(monkeypatch)
    monkeypatch.setattr(instsci_bridge, "_resolve_doi", lambda _doi: None)
    captured = {}

    def fallback(*_args, **kwargs):
        captured.update(kwargs)
        return {"doi": doi, "identifier": doi, "success": False}

    monkeypatch.setattr(sources, "download", fallback)

    sources._batch_institutional_phase(
        [doi], tmp_path, {"scihub_enabled": True}, "batch", {doi: {"success": False}},
        scihub_enabled=False, use_vpnsci=False,
    )

    assert captured["scihub_enabled"] is False
    assert captured["use_vpnsci"] is False


def test_batch_download_passes_explicit_authorization_to_phase_two(monkeypatch, tmp_path):
    from scansci_pdf import identifiers as identifier_module

    doi = "10.1/a1"
    monkeypatch.setattr(sources, "load_config", lambda: {
        "batch_workers": 1,
        "batch_stagger_seconds": 0,
        "batch_total_timeout": 5,
        "output_dir": str(tmp_path),
    })
    monkeypatch.setattr(identifier_module, "validate_doi", lambda _doi: (True, ""))
    monkeypatch.setattr(sources, "_load_progress", lambda _batch_id: {})
    monkeypatch.setattr(sources, "_write_download_results", lambda *args, **kwargs: None)
    phase_one = {}

    def phase_one_download(_doi, _output_dir, **kwargs):
        phase_one.update(kwargs)
        return {"doi": doi, "identifier": doi, "success": False, "error": "phase one"}

    monkeypatch.setattr(sources, "download", phase_one_download)
    phase_two = {}
    monkeypatch.setattr(
        sources, "_batch_institutional_phase",
        lambda *args, **kwargs: phase_two.update(kwargs),
    )

    sources.batch_download(
        [doi], tmp_path, resume=False, scihub_enabled=False, use_vpnsci=False,
    )

    assert phase_one["scihub_enabled"] is False
    assert phase_one["use_vpnsci"] is False
    assert phase_two["scihub_enabled"] is False
    assert phase_two["use_vpnsci"] is False


def test_instsci_browser_source_uses_the_global_browser_budget():
    assert "InstSci" in sources._BROWSER_SOURCE_LABELS


@pytest.mark.parametrize(
    "profile,alias",
    [(profile, alias) for profile in publisher_profiles.PUBLISHER_PROFILES.values()
     for alias in profile.aliases],
)
def test_every_legacy_alias_broker_is_discovered(monkeypatch, tmp_path, profile, alias):
    doi = "10.1/a1"
    alias_key = session_broker.broker_key(alias.strip().lower().replace(" ", "-"))
    _profile_inference(monkeypatch, {doi: profile})
    monkeypatch.setattr(session_broker, "broker_is_running", lambda key: key == alias_key)
    monkeypatch.setattr(
        publisher_batch, "PublisherBatchDownloader",
        lambda **kwargs: pytest.fail("a legacy alias broker must be reused"),
    )
    jobs = []

    def submit(**kwargs):
        jobs.append(kwargs["publisher"])
        records = [SimpleNamespace(**record) for record in kwargs["records"]]
        _write_manifest(kwargs["output_dir"], records, success=True)
        return {}

    monkeypatch.setattr(session_broker, "submit_broker_job", submit)
    results = {doi: {"success": False}}

    sources._batch_institutional_phase([doi], tmp_path, {}, "batch", results)

    assert jobs == [alias_key]
    assert results[doi]["success"]


@pytest.mark.parametrize("failure", [TimeoutError("late"), RuntimeError("unknown")])
def test_public_batch_retains_broker_outcome_failures(monkeypatch, tmp_path, failure):
    from scansci_pdf import identifiers as identifier_module

    doi = "10.1/a1"
    acs = _profile("ACS")
    state_dir = tmp_path / "state"
    batch_id = f"broker-{type(failure).__name__}"
    monkeypatch.setattr(sources, "DATA_DIR", state_dir)
    monkeypatch.setattr(sources, "_save_progress", _REAL_SAVE_PROGRESS)
    monkeypatch.setattr(sources, "load_config", lambda: {
        "batch_workers": 1,
        "batch_stagger_seconds": 0,
        "batch_total_timeout": 5,
        "output_dir": str(tmp_path),
    })
    monkeypatch.setattr(identifier_module, "validate_doi", lambda _doi: (True, ""))
    _profile_inference(monkeypatch, {doi: acs})
    monkeypatch.setattr(session_broker, "broker_is_running", lambda _key: True)
    monkeypatch.setattr(
        session_broker, "submit_broker_job",
        lambda **kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        publisher_batch, "PublisherBatchDownloader",
        lambda **kwargs: pytest.fail("an unresolved broker outcome must not start direct"),
    )

    def phase_one_download(_doi, _output_dir, **_kwargs):
        return {"doi": doi, "identifier": doi, "success": False, "error": "phase one"}

    monkeypatch.setattr(sources, "download", phase_one_download)

    summary = sources.batch_download(
        [doi], tmp_path, resume=False, batch_id=batch_id,
    )

    row = summary["results"][0]
    assert not row["success"]
    assert "broker job" in row["reason"]
    assert "suppressed" in row["reason"]
    progress = (state_dir / "batch_progress" / f"{batch_id}.jsonl").read_text(encoding="utf-8")
    assert row["reason"] in progress
