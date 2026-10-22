"""CLI package — legacy import path.

The canonical CLI is :mod:`scansci_pdf.main` (entry point ``scansci-pdf``).
This package re-exports the older InstSci-era app in ``_cli_core.py`` for
compatibility; new commands belong in ``main.py``.
"""

# Re-export the Typer app and all commands from the core module
from .._cli_core import app  # noqa: F401

# Trigger command registration by importing the core module
from .. import _cli_core  # noqa: F401
