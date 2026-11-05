"""`scansci-pdf est-batch` imports ACSBatchDownloader from .acs_batch.

The compatibility module must re-export every name the CLI imports; #70 was a
missing export that raised `ImportError` before the command did any work.
"""

from __future__ import annotations

import inspect
import re

from scansci_pdf import _cli_core, acs_batch


def test_the_est_batch_import_resolves():
    from scansci_pdf.acs_batch import ACSBatchDownloader
    from scansci_pdf.publisher_batch import ACSBatchDownloader as canonical

    assert ACSBatchDownloader is canonical


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
