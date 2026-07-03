"""Config adapter wrapping the flat config dict for institutional workflows."""

from __future__ import annotations

from typing import Any


class ConfigAdapter:
    """Dict-compatible wrapper around scansci_pdf config.

    Provides ``.load()`` as the canonical entry point and exposes
    ``._config`` for direct dict access when needed (e.g. setting
    ``output_dir``).  Implements enough of the ``dict`` protocol so
    that downstream consumers (``PaperFetcher``, ``EZProxyAuth``,
    ``WebVPNAuth``, ``CARSIClient``, ...) can treat it as a plain
    config dict.
    """

    def __init__(self) -> None:
        from ..config import load_config

        self._config: dict[str, Any] = load_config()

    @classmethod
    def load(cls) -> ConfigAdapter:
        """Return a fully initialised adapter (same as ``ConfigAdapter()``)."""
        return cls()

    @property
    def chrome_profile_dir(self) -> str:
        """Path to the Chrome/Chromium profile directory."""
        return self._config.get("chrome_profile_dir", "")

    # dict protocol – makes the adapter transparent to downstream code
    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._config[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._config[key] = value

    def __contains__(self, key: str) -> bool:  # type: ignore[override]
        return key in self._config

    def __iter__(self):  # type: ignore[override]
        return iter(self._config)

    def keys(self):
        return self._config.keys()

    def items(self):
        return self._config.items()

    def values(self):
        return self._config.values()

    def __len__(self) -> int:
        return len(self._config)

    def __repr__(self) -> str:
        return f"ConfigAdapter({self._config!r})"
