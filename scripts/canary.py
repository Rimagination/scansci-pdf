"""Lightweight live canaries for lenient publisher surfaces.

Run in CI (weekly, .github/workflows/canary.yml) to catch silent route rot:
a failing check usually means a publisher changed their page structure or
started blocking plain HTTP. Exit code 1 alerts the workflow.
"""

from __future__ import annotations

import sys

import requests

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

# (name, url, expected_status, must_be_pdf)
CHECKS = [
    ("arxiv_api", "https://export.arxiv.org/api/query?search_query=all:electron&max_results=1", 200, False),
    ("plos_article_page", "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0065432", 200, False),
    (
        "copernicus_supplement_pdf",
        "https://nhess.copernicus.org/articles/23/2531/2023/nhess-23-2531-2023-supplement.pdf",
        200,
        True,
    ),
    (
        "nature_article_page",
        "https://www.nature.com/articles/s41586-021-03819-2",
        200,
        False,
    ),
]


def main() -> None:
    failures: list[str] = []
    session = requests.Session()
    session.trust_env = False
    for name, url, expected, must_be_pdf in CHECKS:
        try:
            resp = session.get(url, headers=UA, timeout=30, allow_redirects=True, stream=True)
            ok = resp.status_code == expected
            detail = f"HTTP {resp.status_code}"
            if ok and must_be_pdf:
                first = next(resp.iter_content(chunk_size=8192), b"")
                ok = first.startswith(b"%PDF")
                detail += f" | magic={first[:4]!r}"
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
            if not ok:
                failures.append(name)
        except Exception as exc:
            print(f"[FAIL] {name}: {exc.__class__.__name__}: {exc}")
            failures.append(name)
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} canaries passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
