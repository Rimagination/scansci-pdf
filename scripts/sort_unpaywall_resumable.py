#!/usr/bin/env python3
"""Resumable Unpaywall single-DOI crawler for large triage lists.

10 workers / 3 retries / checkpoint after every record. Field test: 5,645 DOIs
in ~21 min at ~4.4 rec/s.

Gotchas baked in:
- Unpaywall rejects placeholder emails with HTTP 422 ("Please use your own
  email address" — the message is in the response body). Pass a real mailbox
  and validate with one DOI before launching the full run.
- The bulk endpoint POST /v2/dois is unreliable (frequent 500s): do not use.

Usage: python sort_unpaywall_resumable.py dois.txt --email you@real.cn -o upw.jsonl
"""
import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Set on the fatal 422 (email rejected) so the whole run stops instead of
# burning every remaining DOI on requests that are doomed to fail.
_abort = threading.Event()


def fetch(doi, email):
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": f"mailto:{email}"})
            with urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
            loc = d.get("best_oa_location") or {}
            return {"doi": doi, "is_oa": bool(d.get("is_oa")),
                    "oa_status": d.get("oa_status"),
                    "pdf_url": loc.get("url_for_pdf"),
                    "landing_url": loc.get("url_for_landing_page"),
                    "host_type": loc.get("host_type")}
        except HTTPError as e:
            if e.code == 404:
                return {"doi": doi, "is_oa": False,
                        "oa_status": "not_in_unpaywall", "pdf_url": None,
                        "landing_url": None, "host_type": None}
            if e.code == 422:
                _abort.set()
                return {"doi": doi, "is_oa": None,
                        "oa_status": "email_rejected_422", "pdf_url": None,
                        "landing_url": None, "host_type": None}
            time.sleep(3 * (attempt + 1))
        except Exception:
            time.sleep(3 * (attempt + 1))
    return {"doi": doi, "is_oa": None, "oa_status": "error", "pdf_url": None,
            "landing_url": None, "host_type": None}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dois", help="text file, one DOI per line")
    ap.add_argument("--email", required=True,
                    help="real mailbox; Unpaywall 422s placeholder addresses")
    ap.add_argument("-o", "--out", default="unpaywall_raw.jsonl")
    ap.add_argument("-j", "--workers", type=int, default=10)
    a = ap.parse_args()

    # fail fast on a rejected email instead of burning the whole run
    ok = fetch("10.1016/j.envres.2024.118134", a.email)
    if ok["oa_status"] in ("error", "email_rejected_422"):
        raise SystemExit("preflight failed: check network / email "
                         "(422 = Unpaywall rejected the email address)")

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

    n_ok = n_err = 0
    with out.open("a", encoding="utf-8") as lf:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(fetch, d, a.email): d for d in todo}
            try:
                for i, fut in enumerate(as_completed(futs), 1):
                    rec = fut.result()
                    lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if rec["oa_status"] == "error":
                        n_err += 1
                    else:
                        n_ok += 1
                    if i % 200 == 0:
                        lf.flush()
                        print(f"progress {i}/{len(todo)} ok={n_ok} err={n_err}",
                              flush=True)
                    if _abort.is_set():
                        lf.flush()
                        raise SystemExit(
                            "Unpaywall 422: email rejected — aborting mid-run. "
                            "Pass a real mailbox and re-run; completed records "
                            "are kept and skipped on resume.")
            finally:
                # cancel queued DOIs so the abort actually stops the run
                ex.shutdown(wait=False, cancel_futures=True)
        lf.flush()
    print(f"FINISHED ok={n_ok} err={n_err}", flush=True)


if __name__ == "__main__":
    main()
