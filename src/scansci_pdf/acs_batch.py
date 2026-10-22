"""Backward-compatible ACS batch imports.

The batch implementation now lives in :mod:`scansci_pdf.publisher_batch` so other
high-volume publishers can reuse the same deterministic browser state machine.
"""

from .publisher_batch import (
    ACSCloakBatchDownloader,
    DownloadResult,
    PaperRecord,
    PublisherBatchDownloader,
    fetch_est_records,
    safe_name,
)

__all__ = [
    "ACSCloakBatchDownloader",
    "DownloadResult",
    "PaperRecord",
    "PublisherBatchDownloader",
    "fetch_est_records",
    "safe_name",
]
