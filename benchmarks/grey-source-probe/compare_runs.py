#!/usr/bin/env python3
"""Compare two grey-source probe reports.

Usage:
    python benchmarks/grey-source-probe/compare_runs.py results/a.json results/b.json

Prints aggregate deltas plus the DOIs that changed state.  Exit code is 0
when both reports are readable, regardless of the hit rates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rows(report: dict) -> dict[str, dict]:
    return {r["doi"].lower(): r for r in report.get("results", []) if r.get("doi")}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2

    a, b = _load(argv[1]), _load(argv[2])
    ra, rb = _rows(a), _rows(b)
    common = sorted(set(ra) & set(rb))

    print(f"A: {argv[1]}  ({a.get('started_at', '?')}, strategy={a.get('strategy', '?')})")
    print(f"B: {argv[2]}  ({b.get('started_at', '?')}, strategy={b.get('strategy', '?')})")
    print()

    for label, report in (("A", a), ("B", b)):
        s = report.get("summary", {})
        print(f"{label}: {s.get('succeeded', 0)}/{s.get('total', 0)} "
              f"({float(s.get('hit_rate', 0)) * 100:.1f}%)  sources={s.get('by_source', {})}")

    gained = [d for d in common if rb[d].get("success") and not ra[d].get("success")]
    lost = [d for d in common if ra[d].get("success") and not rb[d].get("success")]
    print(f"\nCommon DOIs: {len(common)}  gained={len(gained)}  lost={len(lost)}")
    for doi in gained:
        print(f"  + {doi}  {rb[doi].get('source')}")
    for doi in lost:
        print(f"  - {doi}")

    print("\n| DOI | A | B | A source | B source |")
    print("|---|---|---|---|---|")
    for doi in common:
        print(f"| {doi} | {'OK' if ra[doi].get('success') else 'FAIL'} "
              f"| {'OK' if rb[doi].get('success') else 'FAIL'} "
              f"| {ra[doi].get('source') or '-'} | {rb[doi].get('source') or '-'} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
