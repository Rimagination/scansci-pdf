"""The PDF magic number is five bytes: ``b"%PDF-"``.

Comparing a shorter slice against it is *always* false, so the guarded write
never runs and an otherwise valid, already-downloaded PDF is silently
discarded. That defect shipped three separate times (CARSI capture, WebVPN
capture, and the two WebVPN download-event fallbacks) and each instance was
invisible to the runtime tests, because reaching any of those branches needs
a live authenticated session.

So the invariant is checked statically instead.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

# e.g. body[:4] == b"%PDF-"  /  pdf_bytes[:3] != b"%PDF-"
SHORT_SLICE_VS_SIGNATURE = re.compile(r'\[:\s*[0-4]\s*\]\s*(?:==|!=)\s*b"%PDF-"')


def test_no_slice_shorter_than_the_signature_is_compared_against_it():
    offenders = [
        f"{path.relative_to(REPO)}:{lineno}: {line.strip()}"
        for path in sorted(SRC.rglob("*.py"))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        )
        if SHORT_SLICE_VS_SIGNATURE.search(line)
    ]
    assert not offenders, (
        "always-false PDF signature check(s); compare all five bytes, "
        'e.g. body.startswith(b"%PDF-") or body[:5] == b"%PDF-":\n'
        + "\n".join(offenders)
    )
