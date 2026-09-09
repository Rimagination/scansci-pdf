#!/usr/bin/env python3
"""Finalize a triage run: bucket files, UTF-8 BOM CSV report, Excel write-back.

Input is a triage JSONL where each line is:
    {"doi": "...", "cat": "OA-开源|仓库有全文|需机构权限", "link": "..."|null}

Plus one or more screening workbooks (WOS full-record exports) whose rows carry
the same DOIs. Adds/updates two columns — 获取分诊 (bucket) and 已下载/编号
(human key) — and writes bucket files `oa.txt` / `repo.txt` / `institution.txt`.

Design notes:
- Write-back uses the sheet's own human key (e.g. WOS `Serial No.ID`) if
  present: screening teams think in that key, not in DOIs. Cell value becomes
  `{prefix}{key}` so PDF filenames (`{prefix}{key}_{doi_normalized}.pdf`) are
  findable by search.
- Excel saves can take minutes on full-record sheets (78 cols x 6k rows); run
  AFTER all download layers finish, and skip files locked by other processes.

Usage:
  python sort_finalize_writeback.py triage.jsonl --xlsx a.xlsx --xlsx b.xlsx \
      --key-col "Serial No.ID" --prefix global --prefix collection \
      --out-dir report/
"""
import argparse
import csv
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("triage", help="JSONL: {doi, cat, link}")
    ap.add_argument("--xlsx", action="append", default=[],
                    help="screening workbook to write back (repeatable)")
    ap.add_argument("--sheet", default=None, help="sheet name (default: active)")
    ap.add_argument("--key-col", default=None,
                    help="human key column, e.g. 'Serial No.ID'")
    ap.add_argument("--prefix", action="append", default=[],
                    help="human-key prefix per --xlsx, same order (e.g. global)")
    ap.add_argument("--out-dir", default="report")
    a = ap.parse_args()

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        raise SystemExit("pip install openpyxl")

    outdir = Path(a.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    rows = {}
    for line in Path(a.triage).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        rows[r["doi"].strip().lower()] = r

    # bucket files
    buckets = {"OA-开源": "oa", "仓库有全文": "repo", "需机构权限": "institution"}
    for cat, name in buckets.items():
        with (outdir / f"{name}.txt").open("w", encoding="utf-8") as f:
            for doi, r in rows.items():
                if r["cat"] == cat:
                    f.write(f"{doi}\t{r.get('link') or ''}\n")

    # BOM CSV report
    cnt = {}
    for r in rows.values():
        cnt[r["cat"]] = cnt.get(r["cat"], 0) + 1
    with (outdir / "triage_report.csv").open("w", encoding="utf-8-sig",
                                             newline="") as f:
        w = csv.writer(f)
        w.writerow(["分桶", "篇数"])
        for k, v in sorted(cnt.items(), key=lambda x: -x[1]):
            w.writerow([k, v])

    # Excel write-back: two columns — 获取分诊 (bucket) + 已下载/编号 (human key)
    fills = {"OA-开源": "C6EFCE", "仓库有全文": "E2EFDA", "需机构权限": "FFEB9C"}
    for i, xls in enumerate(a.xlsx):
        prefix = a.prefix[i] if i < len(a.prefix) else None
        wb = openpyxl.load_workbook(xls)
        ws = wb[a.sheet] if a.sheet else wb.active
        head = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
        if "DOI" not in head:
            raise SystemExit(f"{xls}: no 'DOI' column in the header row")
        doi_col = head["DOI"]
        key_col = head.get(a.key_col) if a.key_col else None
        col = ws.max_column + 1
        keycol = col + 1 if (prefix and key_col) else None
        ws.cell(1, col, "获取分诊").font = Font(bold=True)
        if keycol:
            ws.cell(1, keycol, "已下载/编号").font = Font(bold=True)
        n = 0
        for ri in range(2, ws.max_row + 1):
            doi = str(ws.cell(ri, doi_col).value or "").strip().lower()
            r = rows.get(doi)
            if not r:
                continue
            ws.cell(ri, col, r["cat"])
            if fills.get(r["cat"]):
                ws.cell(ri, col).fill = PatternFill("solid",
                                                    fgColor=fills[r["cat"]])
            if keycol:
                ws.cell(ri, keycol, f"{prefix}{ws.cell(ri, key_col).value}")
            n += 1
        wb.save(xls)
        print(f"{Path(xls).name}: wrote {n} rows", flush=True)
    print(f"buckets + CSV in {outdir}/", flush=True)


if __name__ == "__main__":
    main()
