"""Shared cookie persistence for institutional access sessions."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CookieStore:
    """Persist browser cookies and load them into requests sessions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Load valid cookies from disk."""
        if not self.path.exists():
            return []

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read cookies from %s: %s", self.path, exc)
            return []

        if not isinstance(raw, list):
            logger.warning("Cookie file %s does not contain a cookie list", self.path)
            return []

        now = time.time() if now is None else now
        cookies = [self._normalize_cookie(c) for c in raw if isinstance(c, dict)]
        return [c for c in cookies if self._is_valid(c, now)]

    def save(self, cookies: list[dict[str, Any]], *, now: float | None = None) -> list[dict[str, Any]]:
        """Save valid cookies to disk and return the persisted list."""
        now = time.time() if now is None else now
        valid = [
            normalized
            for cookie in cookies
            if isinstance(cookie, dict)
            for normalized in [self._normalize_cookie(cookie)]
            if self._is_valid(normalized, now)
        ]

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(valid, indent=2, ensure_ascii=False), encoding="utf-8")
        return valid

    def load_into(self, session: Any, *, now: float | None = None) -> bool:
        """Load valid cookies into a requests-like session."""
        cookies = self.load(now=now)
        self.apply_to_session(session, cookies)
        return bool(cookies)

    @staticmethod
    def apply_to_session(session: Any, cookies: list[dict[str, Any]]) -> None:
        """Apply cookies to a requests-like session cookie jar."""
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if name is None or value is None:
                continue
            session.cookies.set(
                name,
                value,
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

    @staticmethod
    def _normalize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(cookie)
        if normalized.get("expires", 0) < 0:
            normalized["expires"] = 0
        return normalized

    @staticmethod
    def _is_valid(cookie: dict[str, Any], now: float) -> bool:
        if cookie.get("name") is None or cookie.get("value") is None:
            return False

        expires = cookie.get("expires", 0)
        if not expires or expires == 0:
            return True
        return expires > now
