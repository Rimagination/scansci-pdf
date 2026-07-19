"""Standalone CARSI download source — decoupled from WebVPN.

CARSI can now be used independently: set carsi_enabled=True and carsi_idp_name,
and it will be tried in the download tier system without requiring instsci_enabled.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..log import get_logger

log = get_logger()


def try_carsi(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Try downloading via CARSI federated auth without WebVPN dependency.

    Returns a result dict on success, None on failure.
    """
    if cancel_event is not None and cancel_event.is_set():
        return None
    if not config.get("carsi_enabled", False):
        return None

    idp_name = config.get("carsi_idp_name", "").strip()
    if not idp_name:
        return None

    client = None
    try:
        from .carsi import CARSIClient, detect_publisher
        from .instsci import _resolve_doi_url

        resolved_url = _resolve_doi_url(doi)
        if cancel_event is not None and cancel_event.is_set():
            return None
        if not resolved_url:
            resolved_url = f"https://doi.org/{doi}"

        publisher = detect_publisher(resolved_url)
        if not publisher:
            return None

        log.info(f"   [CARSI] Trying {publisher} via {idp_name} for {doi}")
        client = CARSIClient(config)

        # If resolved URL is on a mirror/proxy domain, rebuild using primary domain
        from urllib.parse import urlparse
        cfg = client._publisher_configs.get(publisher)
        if cfg:
            resolved_host = urlparse(resolved_url).hostname or ""
            primary_domain = cfg.domains[0]
            if resolved_host and primary_domain not in resolved_host:
                # Reconstruct URL using primary domain + same path
                from urllib.parse import urlunparse
                parsed = urlparse(resolved_url)
                resolved_url = urlunparse(parsed._replace(
                    scheme="https", netloc=primary_domain))
                log.info(f"   [CARSI] Redirected to primary domain: {resolved_url[:80]}")

        # Try browser download (CloakBrowser first, Selenium fallback)
        if cancel_event is not None and cancel_event.is_set():
            return None
        result = client.download_via_browser(
            doi,
            resolved_url,
            output_path,
            cancel_event=cancel_event,
        )
        if result:
            return result
    except ImportError:
        return None
    except Exception as e:
        if cancel_event is None or not cancel_event.is_set():
            log.info(f"   [CARSI] {e}")
    finally:
        if client is not None:
            client.close()
    return None
