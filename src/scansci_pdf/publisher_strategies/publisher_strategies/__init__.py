"""Publisher-specific PDF download strategies — plugin architecture.

Each publisher lives in its own module.  Modules auto-register with
:class:`StrategyRegistry` on import.  All legacy ``try_*_browser``
functions are re-exported for backward compatibility.

New code should use :meth:`StrategyRegistry.get_for_doi` or
:meth:`StrategyRegistry.get_by_name` instead of the old dispatch dicts.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------
from .base import BasePublisherStrategy
from .registry import StrategyRegistry

# ---------------------------------------------------------------------------
# Import all publisher modules so they self-register
# ---------------------------------------------------------------------------
from . import acs            # noqa: F401
from . import acm            # noqa: F401
from . import aip            # noqa: F401
from . import aps            # noqa: F401
from . import asce           # noqa: F401
from . import copernicus     # noqa: F401
from . import elsevier       # noqa: F401
from . import generic        # noqa: F401
from . import ieee           # noqa: F401
from . import iop            # noqa: F401
from . import nature         # noqa: F401
from . import oxford         # noqa: F401
from . import royalsociety   # noqa: F401
from . import rsc            # noqa: F401
from . import sage           # noqa: F401
from . import science        # noqa: F401
from . import springer       # noqa: F401
from . import tandfonline    # noqa: F401
from . import wiley          # noqa: F401

# ---------------------------------------------------------------------------
# Backward-compatible re-exports of legacy public API.
#
# IMPORTANT: these are lazy-imported to avoid circular import chains when
# sources/publishers.py calls _elsevier_api_fn() at module load time, which
# triggers a chain: publishers -> publisher_strategies -> _publisher_strategies_core.
# ---------------------------------------------------------------------------


def __getattr__(name: str):
    """Lazy-import legacy symbols from _publisher_strategies_core on first access."""
    _legacy_exports = {
        # Public entry-point functions
        "get_last_error",
        "try_elsevier_api",
        "try_elsevier_browser",
        "try_wiley_browser",
        "try_ieee_browser",
        "try_acs_browser",
        "try_rsc_browser",
        "try_aip_browser",
        "try_springer_browser",
        "try_aps_browser",
        "try_tandfonline_browser",
        "try_iop_browser",
        "try_oxford_browser",
        "try_acm_browser",
        "try_nature_browser",
        "try_science_browser",
        "try_sage_browser",
        "try_asce_browser",
        "try_royalsociety_browser",
        "try_copernicus_direct",
        "try_generic_browser",
        # Internal symbols (consumed by carsi.py, instsci.py, sources/__init__.py)
        "_IDP_MAP",
        "_AUTH_KEYWORDS",
        "_AUTH_TITLES",
        "_INSTITUTION_SEARCH_SELECTORS",
        "_SSO_LINK_FINDER_JS",
        "_INSTITUTION_CLICK_JS",
        "_visible_browser",
        "_save_all_cookie_formats",
        "_school_auth_patterns",
        "_PUBLISHER_SSO_CONFIG",
        "_resolve_elsevier_pii",
        "_extract_elsevier_pdf_attachment_eids",
    }
    if name in _legacy_exports:
        from .. import _publisher_strategies_core as _core
        obj = getattr(_core, name, None)
        if obj is not None:
            return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BasePublisherStrategy",
    "StrategyRegistry",
    "get_last_error",
    "try_elsevier_api",
    "try_elsevier_browser",
    "try_wiley_browser",
    "try_ieee_browser",
    "try_acs_browser",
    "try_rsc_browser",
    "try_aip_browser",
    "try_springer_browser",
    "try_aps_browser",
    "try_tandfonline_browser",
    "try_iop_browser",
    "try_oxford_browser",
    "try_acm_browser",
    "try_nature_browser",
    "try_science_browser",
    "try_sage_browser",
    "try_asce_browser",
    "try_royalsociety_browser",
    "try_copernicus_direct",
    "try_generic_browser",
]
