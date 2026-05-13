"""LibGen source with multi-mirror failover."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

from ..network import fetch, polite_delay
from ..pdf_utils import download_pdf

LIBGEN_MIRRORS = [
    "https://libgen.li",
    "https://libgen.bz",
    "https://libgen.gs",
    "https://libgen.rs",
    "https://libgen.st",
]


def try_libgen(doi: str, output_path: Path, config: dict[str, Any], use_tor: bool = False) -> dict[str, Any] | None:
    q = urllib.parse.quote(doi, safe="")
    for mirror in LIBGEN_MIRRORS:
        result = _try_libgen_mirror(doi, q, mirror, output_path, config, use_tor=use_tor)
        if result:
            return result
    return None


def _try_libgen_mirror(
    doi: str, q: str, mirror: str, output_path: Path, config: dict[str, Any], use_tor: bool = False
) -> dict[str, Any] | None:
    url = f"{mirror}/ads.php?doi={q}"
    try:
        resp = fetch(url, config, use_tor=use_tor)

        # Fallback to camofox on Cloudflare/403
        if resp.status_code in (403, 503):
            from ..flaresolverr import solve_url, is_available
            if is_available(config):
                result = solve_url(url, config)
                if result:
                    solution = result.get("solution", {})
                    if solution.get("status", 0) < 400:
                        resp_content = solution.get("response", "")
                        html = resp_content
                    else:
                        return None
                else:
                    return None
            else:
                return None
        else:
            if resp.status_code >= 400:
                return None
            html = resp.text
        for match in re.finditer(r'''href=["']([^"']*get\.php[^"']+)["']''', html, re.I):
            dl_path = match.group(1)
            dl_url = urllib.parse.urljoin(url, dl_path)
            polite_delay(config)
            result = download_pdf(dl_url, output_path, config, "LibGen", require_pdf_like_url=False, use_tor=use_tor)
            if result:
                result["doi"] = doi
                result["identifier"] = doi
                return result
        for match in re.finditer(r'''href=["']([^"']*\.pdf[^"']*)["']''', html, re.I):
            dl_path = match.group(1)
            if "get.php" in dl_path or dl_path.endswith(".pdf"):
                dl_url = urllib.parse.urljoin(url, dl_path)
                polite_delay(config)
                result = download_pdf(dl_url, output_path, config, "LibGen", require_pdf_like_url=False, use_tor=use_tor)
                if result:
                    result["doi"] = doi
                    result["identifier"] = doi
                    return result
    except Exception:
        pass
    return None
