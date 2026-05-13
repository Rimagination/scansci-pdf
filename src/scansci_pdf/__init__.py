"""ScanSci PDF - Academic paper downloader MCP server."""

__version__ = "1.3.1"

__all__ = [
    "__version__",
    "download",
    "batch_download",
    "search_papers",
    "load_config",
    "update_config",
    "get_config_safe",
    "STRATEGIES",
]

from .sources import download, batch_download, STRATEGIES
from .search import search_papers
from .config import load_config, update_config, get_config_safe
