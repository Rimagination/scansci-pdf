"""CLI package — commands grouped by domain.

The actual command implementations live in ``_cli_core.py`` (for now).
This package exists so future command modules can be added under ``cli/``
without changing the public import path.
"""

# Re-export the Typer app and all commands from the core module
from .._cli_core import app  # noqa: F401

# Trigger command registration by importing the core module
from .. import _cli_core  # noqa: F401
