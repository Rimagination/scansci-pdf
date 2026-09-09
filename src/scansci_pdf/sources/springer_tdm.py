"""Springer Nature Full Text API (TDM): subscription full text for 10.1007.

Institutional lane for Springer papers, mirroring the Elsevier API lane.
There is no PDF endpoint — the TDM API returns the JATS XML full text —
so the artifact is a .xml sibling (non-PDF artifacts keep their extension;
_auto_rename skips them). Entitlement follows the INSTITUTION: the account
must hold a Springer subscription with TDM rights (free key from
dev.springernature.com, affiliated via ORCID). Without entitlement the API
returns no full text and this source degrades to a silent miss.

Endpoint: https://api.springernature.com/xmldata/jats?q=doi:"..."&api_key=...
(the official springernature_api_client sends the key as a query param;
premium keys have the form "key/metric" and work the same way).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SPRINGER_TDM_URL = "https://api.springernature.com/xmldata/jats"

# Known paywalled article (Math. Ann., verified closed via OpenAlex) used to
# probe whether a key carries subscription full-text entitlement.
_ENTITLEMENT_PROBE_DOI = "10.1007/s00208-025-03286-4"


def _session(config: dict[str, Any]):
    import requests

    s = requests.Session()
    s.trust_env = False
    proxy = config.get("network_proxy", "")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    from ..network import USER_AGENT

    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch_fulltext_xml(doi: str, api_key: str,
                       config: dict[str, Any]) -> tuple[str | None, str]:
    """Fetch the JATS XML full text for one DOI.

    Returns (xml, status); status is one of
    ok / not_found / invalid_key / not_entitled / rate_limited / error.
    """
    try:
        resp = _session(config).get(
            SPRINGER_TDM_URL,
            params={"q": f'doi:"{doi}"', "p": 1, "s": 1, "api_key": api_key},
            timeout=(10, 45),
        )
    except Exception as e:
        return None, f"error:{type(e).__name__}"

    if resp.status_code in (401, 403):
        # 401 = bad key; 403 = key valid but no entitlement for this content
        return None, "invalid_key" if resp.status_code == 401 else "not_entitled"
    if resp.status_code == 429:
        return None, "rate_limited"
    if resp.status_code != 200:
        return None, f"error:http_{resp.status_code}"

    xml = resp.text or ""
    if "<body" in xml:
        return xml, "ok"
    # 200 without a body: either the DOI is unknown to the API or the key
    # lacks full-text rights (both surface identically) — not a hard failure.
    return None, "not_entitled" if "<response" in xml else "not_found"


def try_springer_tdm(doi: str, output_path: Path,
                     config: dict[str, Any]) -> dict[str, Any] | None:
    """Racing source: Springer full text via the TDM API (10.1007 only).

    Writes {doi}.xml next to the expected PDF path. PDF lanes (OA direct,
    browser) race in parallel and a PDF win is the better artifact, so this
    source never blocks or shadows them.
    """
    api_key = str(config.get("springer_api_key", "") or "")
    if not api_key:
        return None
    if not doi.lower().startswith("10.1007/"):
        return None

    xml, status = fetch_fulltext_xml(doi, api_key, config)
    if status == "invalid_key":
        from ..pdf_utils import fail

        return fail(
            doi,
            "Springer TDM API key rejected (HTTP 401)",
            error_type="config_needed",
            action="recreate_api_key",
        )
    if status != "ok" or not xml:
        return None

    xml_path = output_path.with_suffix(".xml")
    try:
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml, encoding="utf-8")
    except OSError:
        return None

    from ..pdf_utils import success

    return success(doi, xml_path, "SpringerTDM")


def validate_springer_key(api_key: str,
                          config: dict[str, Any]) -> dict[str, str]:
    """Probe a key with a known paywalled article: does it carry entitlement?

    Used by the setup tool; distinguishes invalid key / valid key without
    subscription full-text rights / entitled.
    """
    if not api_key:
        return {"status": "no_key", "detail": "springer_api_key is empty"}
    xml, status = fetch_fulltext_xml(_ENTITLEMENT_PROBE_DOI, api_key, config)
    if status == "ok":
        return {
            "status": "entitled",
            "detail": "subscription full text available (probe DOI returned JATS body)",
        }
    if status == "invalid_key":
        return {"status": "invalid_key", "detail": "API rejected the key (HTTP 401)"}
    if status == "not_entitled":
        return {
            "status": "not_entitled",
            "detail": (
                "key accepted but no full text for a paywalled article — the "
                "account needs an institutional Springer subscription with TDM "
                "rights (affiliated via ORCID); OA-only keys work only for OA content"
            ),
        }
    return {"status": status, "detail": f"probe returned status={status}"}
