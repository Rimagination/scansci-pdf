"""Shared HTTP utilities with retry logic."""

import logging
import time

import requests

logger = logging.getLogger(__name__)

# Certificate verification stays on. "A proxy environment variable is set" is
# not evidence that the network is trusted: those variables are commonly set
# machine-wide on Windows, and reading them as a licence to skip validation
# turned off certificate checking for every request on this shared retry path
# -- the same path institutional cookies and publisher API keys travel over.
# Suppressing InsecureRequestWarning hid the only signal that said so (#70).
#
# The single case that genuinely needs an exception is a self-signed campus
# connector. That is now explicit and opt-in per call site via
# network.allow_self_signed(config), and it logs when it applies.
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
