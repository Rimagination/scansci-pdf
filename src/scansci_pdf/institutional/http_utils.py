"""Shared HTTP utilities with retry logic."""

import logging
import os
import time
import urllib3

import requests

logger = logging.getLogger(__name__)

# Auto-detect local HTTP connector and disable SSL verification warnings.
if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or \
   os.environ.get("http_proxy") or os.environ.get("https_proxy"):
    _SSL_VERIFY = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
else:
    _SSL_VERIFY = True


def request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    retry_backoff: float = 2.0,
    **kwargs,
) -> requests.Response:
    """HTTP request with exponential backoff retry on 429/5xx/network errors."""
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("verify", _SSL_VERIFY)

    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < max_retries:
                    wait = retry_backoff ** attempt
                    logger.warning(
                        "HTTP %d for %s, retrying in %.1fs (attempt %d/%d)",
                        resp.status_code, url, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue
            return resp
        except requests.RequestException as e:
            if attempt < max_retries:
                wait = retry_backoff ** attempt
                logger.warning(
                    "Request error for %s: %s, retrying in %.1fs (attempt %d/%d)",
                    url, e, wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
            else:
                raise

    return requests.request(method, url, **kwargs)
