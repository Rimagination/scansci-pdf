"""FlareSolverr client for bypassing Cloudflare protection."""

import logging

import requests

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8191/v1"


class FlareSolverrClient:
    """HTTP client that uses FlareSolverr to bypass Cloudflare challenges.

    FlareSolverr must be running as a separate service (Docker or standalone).
    See: https://github.com/FlareSolverr/FlareSolverr
    """

    def __init__(self, base_url: str = DEFAULT_URL):
        self.base_url = base_url.rstrip("/")

    def is_available(self) -> bool:
        """Check if FlareSolverr is running."""
        try:
            resp = requests.get(self.base_url, timeout=3)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def get(self, url: str, wait_seconds: int = 8) -> str | None:
        """Fetch a URL through FlareSolverr, returning HTML content.

        Uses fast-path retry: first tries with wait_seconds=0, then retries
        with the full wait if a challenge is detected.

        Args:
            url: The URL to fetch.
            wait_seconds: Seconds to wait for Cloudflare challenge resolution.

        Returns:
            HTML string if successful, None if failed.
        """
        # Fast path: no wait
        html = self._request(url, wait_seconds=0)
        if html and len(html) > 1000:
            return html

        # Challenge detected or content too short, retry with wait
        logger.info("FlareSolverr fast path failed, retrying with %ds wait...", wait_seconds)
        return self._request(url, wait_seconds=wait_seconds)

    def _request(self, url: str, wait_seconds: int = 8) -> str | None:
        """Make a single FlareSolverr request."""
        payload = {
            "cmd": "request.get",
            "url": url,
            "waitInSeconds": wait_seconds,
            "disableMedia": True,
        }
        try:
            resp = requests.post(
                self.base_url,
                json=payload,
                timeout=wait_seconds + 30,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "ok":
                logger.warning("FlareSolverr returned status: %s", data.get("status"))
                return None

            solution = data.get("solution", {})
            status_code = solution.get("status", 0)
            html = solution.get("response", "")

            if status_code == 200 and html:
                return html

            logger.warning("FlareSolverr solution status: %d", status_code)
            return None

        except requests.RequestException as e:
            logger.warning("FlareSolverr request failed: %s", e)
            return None
        except (KeyError, ValueError) as e:
            logger.warning("FlareSolverr response parse error: %s", e)
            return None
