#!/usr/bin/env python3
"""OA triage via the Semantic Scholar batch endpoint (no email required).

POST https://api.semanticscholar.org/graph/v1/paper/batch?fields=isOpenAccess,openAccessPdf
with {"ids": ["DOI:10.xxxx/...", ...]} — up to 500 ids per request. Returns null
entries for DOIs S2 does not know. 429s are common but clear after 10-20s.

Field test: 5,645 DOIs in 12 batches (~3 min), 5611 found; S2 also resolved
ScienceDirect full-text PDF URLs that Unpaywall only gave as landing pages.

Usage: python sort_s2_oa_batch.py dois.txt -o s2_raw.jsonl
"""
import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = ("https://api.semanticscholar.org/graph/v1/paper/batch"
       "?fields=isOpenAccess,openAccessPdf")


def post(ids):
    req = Request(URL, data=json.dumps({"ids": ids}).encode(),
                  headers={"Content-Type": "application/json",
                           "User-Agent": "scansci-sort/1.0"})
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dois", help="text file, one DOI per line")
    ap.add_argument("-o", "--out", default="s2_raw.jsonl")
    ap.add_argument("--batch", type=int, default=500)
    a = ap.parse_args()

    dois = [d.strip() for d in Path(a.dois).read_text(encoding="utf-8").splitlines()
            if d.strip()]
    done = set()
    out = Path(a.out)
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["doi"])
            except Exception:
                pass
    todo = [d for d in dois if d not in done]
    print(f"total={len(dois)} done={len(done)} todo={len(todo)}", flush=True)

    with out.open("a", encoding="utf-8") as lf:
        for i in range(0, len(todo), a.batch):
            chunk = todo[i:i + a.batch]
            for attempt in range(5):
                try:
                    res = post([f"DOI:{d}" for d in chunk])
                    break
                except HTTPError as e:
                    wait = 10 * (attempt + 1)
                    print(f"batch@{i} HTTP {e.code}, retry in {wait}s", flush=True)
                    time.sleep(wait)
                except Exception as e:
                    print(f"batch@{i} {e!r:.60}, retry in 5s", flush=True)
                    time.sleep(5)
            else:
                raise RuntimeError(f"batch@{i} failed after retries")
            found = 0
            for doi, item in zip(chunk, res):
                if item is None:
                    rec = {"doi": doi, "found": False, "is_oa": None, "pdf_url": None}
                else:
                    pdf = (item.get("openAccessPdf") or {}).get("url")
                    rec = {"doi": doi, "found": True,
                           "is_oa": bool(item.get("isOpenAccess")), "pdf_url": pdf}
                    found += 1
                lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            lf.flush()
            print(f"batch {i // a.batch + 1}/{-(-len(todo) // a.batch)} "
                  f"found={found}/{len(chunk)}", flush=True)
            time.sleep(2)
    print("FINISHED", flush=True)


if __name__ == "__main__":
    main()
