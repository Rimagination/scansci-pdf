"""CARSI client bridge — adapts sources.carsi for PaperFetcher.

The existing CARSIClient in sources.carsi expects a dict config.
PaperFetcher passes a ConfigAdapter. This bridge wraps the constructor.
"""

from __future__ import annotations

from typing import Any

from ..sources.carsi import CARSIClient as _BaseCARSIClient
from ..sources.carsi import detect_publisher  # noqa: F401

__all__ = ["CARSIClient", "detect_publisher"]


class CARSIClient(_BaseCARSIClient):
    """CARSIClient that accepts ConfigAdapter or dict."""

    def __init__(self, config: Any):
        if hasattr(config, "_config"):
            # ConfigAdapter → extract underlying dict
            super().__init__(config._config)
        elif isinstance(config, dict):
            super().__init__(config)
        else:
            super().__init__(vars(config))
