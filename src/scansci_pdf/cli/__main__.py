"""Entry point for ``python -m scansci_pdf.cli``.

``session_broker.start_broker_process`` spawns ``python -m scansci_pdf.cli
session-broker-run``; without this module that spawn died with "No module
named scansci_pdf.cli.__main__", so the broker could never start.

Caveat: ``_cli_core.app`` is not wired to any console script -- the shipped
``scansci-pdf`` entry point is ``scansci_pdf.main:main`` -- so the broker
command is still not reachable from the installed CLI.
"""

from .._cli_core import app

if __name__ == "__main__":
    app()
