"""Re-export the wiley publisher adapter from the root package.

The publisher stack shipped in two copies that drifted apart; this module is
now a re-export shim over the single implementation in
``scansci_pdf.extractors.publisher_adapters.wiley``
(#71 item 5). Keep the implementation there, not here.

Nothing is bound at import time. A reload of the canonical module replaces its
function objects, and a shim that captured them at import time would keep
handing back the stale ones -- so every name is resolved through
``__getattr__`` instead, and ``__all__`` is derived from the canonical module
on demand.
"""

from ....extractors.publisher_adapters import wiley as _canonical


def __getattr__(name: str):
    """Resolve every name through the canonical module.

    The old copy exposed the canonical module''s own imported names as its
    attributes too (``requests``, ``time`` and friends), and code that patched
    or read one of them through this path must keep working.
    """
    if name == "__all__":
        return [n for n in dir(_canonical) if not n.startswith("_")]
    try:
        return getattr(_canonical, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(dir(_canonical)) | {"__getattr__", "__dir__", "__all__"})
