"""OpenAIRE aggregator source: green OA / repository copies via api.openaire.eu.

OpenAIRE aggregates repository copies (green OA, institutional repositories,
EU-funded deposits) that Unpaywall's index sometimes misses — its webresource
URLs are nested at varying depths, so extraction walks the payload generically.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..network import fetch_json, polite_delay
from ..pdf_utils import download_pdf

_WEBRESOURCE_RE = re.compile(r"<webresource>\s*<url>([^<]+)</url>", re.I)


def extract_openaire_fulltext_urls(payload: Any) -> list[str]:
    """Recursively collect fulltext URLs from an OpenAIRE payload, PDF-ish first."""
    urls: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("url", "fulltext") and isinstance(value, str) and value.startswith("http"):
                    urls.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    seen: set[str] = set()
    ordered: list[str] = []
    for u in sorted(urls, key=lambda u: 0 if ".pdf" in u.lower() else 1):
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def _extract_from_xml(xml: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    from html import unescape

    for u in _WEBRESOURCE_RE.findall(xml):
        u = unescape(u).strip()
        if u.startswith("http") and u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def try_openaire(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Race source: download an OA copy listed by the OpenAIRE aggregator."""
    # XML is the proven shape: webresource URLs nest under results. The
    # format=json variant does not expose url keys in a walkable way.
    candidates = []
    try:
        import requests

        from ..network import USER_AGENT

        sess = requests.Session()
        sess.trust_env = False
        proxy = (config or {}).get("network_proxy", "")
        r = sess.get(
            f"https://api.openaire.eu/search/publications?doi={quote(doi, safe='')}",
            headers={"User-Agent": USER_AGENT},
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=20,
        )
        if r.status_code == 200:
            candidates = _extract_from_xml(r.text)
    except Exception:
        pass

    if not candidates:
        # Last resort: the format=json response (structure varies) — walk it.
        payload = fetch_json(
            f"https://api.openaire.eu/search/publications?doi={quote(doi, safe='')}&format=json",
            config,
        )
        if payload:
            candidates = extract_openaire_fulltext_urls(payload)
    if not candidates:
        # The legacy endpoint sometimes answers JSON requests with XML —
        # parse that too before giving up.
        try:
            import requests

            from ..network import USER_AGENT

            s = requests.Session()
            s.trust_env = False
            proxy = (config or {}).get("network_proxy", "")
            r = s.get(
                f"https://api.openaire.eu/search/publications?doi={quote(doi, safe='')}",
                headers={"User-Agent": USER_AGENT},
                proxies={"http": proxy, "https": proxy} if proxy else None,
                timeout=20,
            )
            if r.status_code == 200:
                candidates = _extract_from_xml(r.text)
        except Exception:
            pass

    for url in candidates[:5]:
        polite_delay(config)
        # require_pdf_like_url=False: OpenAIRE repository URLs often have no
        # file extension (PLOS file?id=...); the %PDF check inside
        # download_pdf is the real validation.
        result = download_pdf(url, output_path, config, "OpenAIRE", require_pdf_like_url=False)
        if result:
            result["doi"] = doi
            result["identifier"] = doi
            return result
    return None
