"""Optional Cython-accelerated core modules.

The .pyx sources live in this package and are MIT licensed. When Cython
is available they are compiled to .pyd/.so at build time; pure Python fallbacks
exist in the original modules.

Usage in other modules:
    try:
        from .._core.racing import run_parallel_race
    except ImportError:
        # Pure Python fallback is used inline
"""

# Individual compiled modules are imported directly by their callers via
# try/except; there is intentionally no package-level re-export layer.

def _check_availability() -> dict[str, bool]:
    """Check which compiled modules are available."""
    status = {}
    for mod_name in ("racing", "scihub_core", "instsci_core"):
        try:
            __import__(f"scansci_pdf._core.{mod_name}")
            status[mod_name] = True
        except ImportError:
            status[mod_name] = False
    return status
