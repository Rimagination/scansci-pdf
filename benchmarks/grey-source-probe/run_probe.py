#!/usr/bin/env python3
"""Run the closed-access grey-source probe.

Every DOI in ``papers.json`` is attempted with a fresh ScanSci PDF state
directory, so a cached, institutional, or OA download cannot be mistaken for a
grey-source hit.  The default strategy is ``scihub_only`` (Sci-Hub only); pass
``--strategy grey_only`` to include SciBban and LibGen as well.

The PDFs are written under the run directory and are never part of the
repository.  Only the JSON report is meant to be shared.

Usage:
    python benchmarks/grey-source-probe/run_probe.py
    python benchmarks/grey-source-probe/run_probe.py --strategy grey_only
    python benchmarks/grey-source-probe/run_probe.py --limit 5 --output C:/tmp/probe

Then compare two runs:
    python benchmarks/grey-source-probe/compare_runs.py run-a.json run-b.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_PAPERS = BENCH_DIR / "papers.json"
DEFAULT_DATA_ROOT = BENCH_DIR / "runs"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("scansci-pdf")
    except Exception:
        return "unknown"


def _load_papers(path: Path) -> list[dict]:
    papers = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(papers, list) or not papers:
        raise SystemExit(f"No papers found in {path}")
    seen = set()
    for paper in papers:
        doi = str(paper.get("doi", "")).strip()
        if not doi:
            raise SystemExit(f"Paper without DOI in {path}")
        key = doi.lower()
        if key in seen:
            raise SystemExit(f"Duplicate DOI in {path}: {doi}")
        seen.add(key)
    return papers


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _summarise(results: list[dict]) -> dict:
    total = len(results)
    succeeded = sum(1 for r in results if r.get("success"))
    by_publisher = defaultdict(lambda: {"total": 0, "succeeded": 0})
    by_decade = defaultdict(lambda: {"total": 0, "succeeded": 0})
    sources = Counter()
    for row in results:
        pub = row.get("publisher") or "unknown"
        decade = f"{int(row['year']) // 10 * 10}s" if row.get("year") else "unknown"
        by_publisher[pub]["total"] += 1
        by_decade[decade]["total"] += 1
        if row.get("success"):
            by_publisher[pub]["succeeded"] += 1
            by_decade[decade]["succeeded"] += 1
            sources[row.get("source") or "unknown"] += 1
    elapsed = [r["elapsed_s"] for r in results if isinstance(r.get("elapsed_s"), (int, float))]
    return {
        "total": total,
        "succeeded": succeeded,
        "failed": total - succeeded,
        "hit_rate": round(succeeded / total, 4) if total else 0.0,
        "elapsed_s_total": round(sum(elapsed), 1),
        "elapsed_s_median": round(statistics.median(elapsed), 1) if elapsed else None,
        "by_source": dict(sources),
        "by_publisher": {k: {**v, "hit_rate": round(v["succeeded"] / v["total"], 4)}
                         for k, v in sorted(by_publisher.items())},
        "by_decade": {k: {**v, "hit_rate": round(v["succeeded"] / v["total"], 4)}
                      for k, v in sorted(by_decade.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--papers", default=str(DEFAULT_PAPERS), help="Path to papers.json")
    parser.add_argument(
        "--output",
        default="",
        help="Run directory (default: benchmarks/grey-source-probe/runs/<UTC timestamp>)",
    )
    parser.add_argument(
        "--strategy",
        default="scihub_only",
        choices=["scihub_only", "grey_only", "scihub_first", "oa_first", "fastest", "legal_only"],
        help="Download strategy (default: scihub_only)",
    )
    parser.add_argument("--tor", action="store_true", help="Route grey sources through Tor")
    parser.add_argument("--limit", type=int, default=0, help="Only run the first N papers (0 = all)")
    parser.add_argument("--resume", action="store_true", help="Skip DOIs already present in this run's results")
    args = parser.parse_args()

    papers = _load_papers(Path(args.papers))
    if args.limit:
        papers = papers[: args.limit]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.output) if args.output else DEFAULT_DATA_ROOT / run_id
    state_dir = run_dir / "state"
    pdf_dir = run_dir / "pdfs"
    report_path = run_dir / "probe_results.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    # Must be set before scansci_pdf.config is imported: DATA_DIR is resolved at
    # import time.  A fresh directory keeps the probe honest on reruns.
    os.environ["SCANSCI_PDF_DATA_DIR"] = str(state_dir)

    from scansci_pdf.sources import download  # noqa: E402  (import after env)

    previous: dict[str, dict] = {}
    if args.resume and report_path.exists():
        try:
            old = json.loads(report_path.read_text(encoding="utf-8"))
            previous = {r["doi"].lower(): r for r in old.get("results", []) if r.get("doi")}
            print(f"Resuming: {len(previous)} result(s) already recorded")
        except Exception as exc:
            print(f"Could not read previous results ({exc}); starting fresh")

    started_at = _utc_now()
    results: list[dict] = []
    for i, paper in enumerate(papers, 1):
        doi = paper["doi"]
        if args.resume and doi.lower() in previous:
            results.append(previous[doi.lower()])
            print(f"[{i}/{len(papers)}] skip (resumed) {doi}")
            continue

        print(f"[{i}/{len(papers)}] {doi} — {paper.get('title', '')[:70]}")
        t0 = time.time()
        try:
            raw = download(
                doi,
                pdf_dir,
                scihub_enabled=True,
                use_tor=args.tor,
                use_vpnsci=False,
                bibtex=False,
                strategy=args.strategy,
            )
        except Exception as exc:  # keep the run going on a single bad DOI
            raw = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        elapsed = round(time.time() - t0, 1)

        file_path = raw.get("file") or ""
        row = {
            "doi": doi,
            "title": paper.get("title"),
            "first_author": paper.get("first_author"),
            "venue": paper.get("venue"),
            "publisher": paper.get("publisher"),
            "year": paper.get("year"),
            "oa_status": paper.get("oa_status"),
            "success": bool(raw.get("success")),
            "source": raw.get("source"),
            "file_name": Path(file_path).name if file_path else None,
            "size_kb": raw.get("size_kb"),
            "cached": bool(raw.get("cached")),
            "error": raw.get("error") or raw.get("reason"),
            "elapsed_s": elapsed,
        }
        results.append(row)
        status = "OK  " if row["success"] else "FAIL"
        print(f"         {status} source={row['source'] or '-'} elapsed={elapsed}s"
              + (f" error={row['error']}" if row["error"] else ""))

    finished_at = _utc_now()
    summary = _summarise(results)
    report = {
        "benchmark": "closed-access grey-source probe",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "tool_version": _tool_version(),
        "strategy": args.strategy,
        "use_tor": args.tor,
        "state_dir": str(state_dir),
        "pdf_dir": str(pdf_dir),
        "summary": summary,
        "results": results,
    }
    _atomic_write_json(report_path, report)

    print("\n=== Summary ===")
    print(f"Hit rate: {summary['succeeded']}/{summary['total']} "
          f"({summary['hit_rate'] * 100:.1f}%)")
    print(f"Sources: {summary['by_source'] or '-'}")
    print(f"Report: {report_path}")
    return 0 if summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
