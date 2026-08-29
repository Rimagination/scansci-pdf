"""Self-hosted hand-drawn star history.

Fetches the repo's stargazers timeline with the repo's own credentials
(GITHUB_TOKEN — owners/collaborators still can after the 2026-06 API
restriction), builds a daily cumulative curve, and renders an xkcd-style
SVG (wobbly lines, cursive font, currentColor so GitHub dark mode works).

Zero third-party dependencies: stdlib only.

Local run:  GITHUB_TOKEN=$(gh auth token) python scripts/update_star_history.py
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "Rimagination/scansci-pdf")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("STAR_HISTORY_TOKEN") or ""
SVG_PATH = "star-history.svg"
DATA_PATH = "stars.json"

W, H = 860, 420
MARGIN = {"l": 66, "r": 26, "t": 26, "b": 54}
FONT = "'Comic Sans MS','Comic Neue','Segoe Print',cursive"


def fetch_stargazers(repo: str, token: str) -> list[str]:
    times: list[str] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repo}/stargazers"
            f"?per_page=100&page={page}"
        )
        headers = {"Accept": "application/vnd.github.star+json", "User-Agent": "scansci-pdf-star-history"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                batch = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                print(
                    f"GitHub returned {e.code} — the stargazers API is restricted to "
                    "admins/collaborators. Add a repo-scoped read token as the "
                    "STAR_HISTORY_TOKEN secret and re-run."
                )
            raise
        times += [item["starred_at"] for item in batch]
        if len(batch) < 100:
            break
        page += 1
        time.sleep(0.2)
    return sorted(times)


def daily_counts(times: list[str]) -> list[tuple[str, int]]:
    per_day: dict[str, int] = {}
    for t in times:
        d = t[:10]
        per_day[d] = per_day.get(d, 0) + 1
    if not per_day:
        return []
    day = datetime.fromisoformat(min(per_day)).date()
    today = datetime.now(timezone.utc).date()
    out: list[tuple[str, int]] = []
    cur = 0
    while day <= today:
        cur += per_day.get(day.isoformat(), 0)
        out.append((day.isoformat(), cur))
        day += timedelta(days=1)
    return out


# --- hand-drawn rendering (pure SVG, currentColor => adapts to dark mode) -----

RNG = random.Random(42)  # fixed seed: same data -> same wobble -> stable git diffs


def _wobble_path(pts: list[tuple[float, float]], amp: float = 2.2, step: float = 16) -> str:
    dense: list[tuple[float, float]] = []
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        n = max(1, int(max(abs(x2 - x1), abs(y2 - y1)) / step))
        for i in range(n):
            t = i / n
            dense.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    dense.append(pts[-1])
    d = f"M{dense[0][0]:.1f} {dense[0][1]:.1f}"
    for x, y in dense[1:]:
        d += f" L{x + RNG.uniform(-amp, amp):.1f} {y + RNG.uniform(-amp, amp):.1f}"
    return d


def _nice_ceil(v: float) -> int:
    step = 200 if v > 600 else 100 if v > 200 else 50
    return int((v // step + 1) * step)


def render_svg(series: list[tuple[str, int]]) -> str:
    plot_w = W - MARGIN["l"] - MARGIN["r"]
    plot_h = H - MARGIN["t"] - MARGIN["b"]
    y_max = _nice_ceil(max(c for _, c in series) or 1)
    t0 = datetime.fromisoformat(series[0][0]).date()
    t1 = datetime.fromisoformat(series[-1][0]).date()
    span = max((t1 - t0).days, 1)

    def xy(date: str, count: int) -> tuple[float, float]:
        dx = (datetime.fromisoformat(date).date() - t0).days / span
        x = MARGIN["l"] + dx * plot_w
        y = MARGIN["t"] + plot_h * (1 - count / y_max)
        return x, y

    curve = [xy(d, c) for d, c in series]
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="{FONT}" font-size="17">',
        # axes with slight xkcd overshoot
        f'<path d="{_wobble_path([(MARGIN["l"] - 8, MARGIN["t"] - 6), (MARGIN["l"], MARGIN["t"] + plot_h + 8)])}" '
        'stroke="currentColor" fill="none" stroke-width="2"/>',
        f'<path d="{_wobble_path([(MARGIN["l"] - 10, MARGIN["t"] + plot_h), (MARGIN["l"] + plot_w + 10, MARGIN["t"] + plot_h)])}" '
        'stroke="currentColor" fill="none" stroke-width="2"/>',
        # the star curve
        f'<path d="{_wobble_path(curve, amp=2.6, step=14)}" stroke="currentColor" fill="none" stroke-width="2.6"/>',
    ]
    # y ticks
    for i in range(1, 4):
        v = round(y_max * i / 3)
        _, y = xy(series[0][0], v)
        parts.append(
            f'<text x="{MARGIN["l"] - 12}" y="{y + 6}" text-anchor="end" fill="currentColor">{v}</text>'
        )
    # x ticks: ~4 evenly spaced dates
    n = len(series)
    for frac in (0.02, 0.34, 0.67, 0.99):
        d, _ = series[min(int(frac * (n - 1)), n - 1)]
        x, y = xy(d, 0)
        label = datetime.fromisoformat(d).strftime("%b")
        parts.append(
            f'<text x="{x:.0f}" y="{MARGIN["t"] + plot_h + 26}" text-anchor="middle" fill="currentColor">{label}</text>'
        )
    # hand-drawn legend box, top-left
    lx, ly, lw, lh = MARGIN["l"] + 26, MARGIN["t"] + 12, 300, 40
    parts.append(
        f'<path d="{_wobble_path([(lx, ly), (lx + lw, ly), (lx + lw, ly + lh), (lx, ly + lh), (lx, ly)], amp=1.6)}" '
        'stroke="currentColor" fill="none" stroke-width="1.6"/>'
    )
    parts.append(
        f'<circle cx="{lx + 18}" cy="{ly + lh / 2}" r="5" fill="currentColor"/>'
        f'<text x="{lx + 32}" y="{ly + lh / 2 + 6}" fill="currentColor">Rimagination/scansci-pdf</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    times = fetch_stargazers(REPO, TOKEN)
    series = daily_counts(times)
    if not series:
        print("no stargazer data — nothing to render")
        sys.exit(1)
    svg = render_svg(series)
    with open(SVG_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(svg)
    with open(DATA_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"repo": REPO, "updated": series[-1][0], "stars": series[-1][1],
                   "daily": [{"date": d, "stars": c} for d, c in series]},
                  f, ensure_ascii=False, indent=1)
    print(f"{REPO}: {series[-1][1]} stars over {len(series)} days -> {SVG_PATH}")


if __name__ == "__main__":
    import sys

    main()
