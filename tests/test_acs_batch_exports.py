"""`scansci-pdf est-batch` imports ACSCloakBatchDownloader from .acs_batch.

The compatibility module re-exported five names and not that one, so the command
raised `ImportError: cannot import name 'ACSCloakBatchDownloader'` immediately,
before doing any work (#70).
"""

from __future__ import annotations

import inspect
import re

from scansci_pdf import _cli_core, acs_batch


def test_the_est_batch_import_resolves():
    from scansci_pdf.acs_batch import ACSCloakBatchDownloader
    from scansci_pdf.publisher_batch import ACSCloakBatchDownloader as canonical

    assert ACSCloakBatchDownloader is canonical


def test_every_name_the_cli_imports_from_acs_batch_is_exported():
    """Contract test over the real import statements, so the next name added to
    the compatibility module cannot be forgotten in the same way."""
    source = inspect.getsource(_cli_core)
    wanted: set[str] = set()
    for match in re.finditer(r"from \.acs_batch import ([^\n(]+)", source):
        wanted |= {name.strip() for name in match.group(1).split(",") if name.strip()}

    assert wanted, "expected _cli_core to import from .acs_batch"
    missing = sorted(name for name in wanted if not hasattr(acs_batch, name))
    assert not missing, f".acs_batch does not export: {missing}"


def test_all_declared_exports_exist():
    missing = sorted(name for name in acs_batch.__all__ if not hasattr(acs_batch, name))
    assert not missing, f"__all__ names that are not importable: {missing}"
