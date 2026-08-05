"""HTML extraction router - dispatches to publisher-specific adapters."""

import logging

from .publisher_adapters import acs, elsevier, generic, nature, rsc, tandfonline, wiley

logger = logging.getLogger(__name__)

# Ordered list of publisher adapters (checked in order)
_ADAPTERS = [nature, elsevier, wiley, acs, rsc, tandfonline]


def extract(html: str, url: str = "") -> dict:
    """Extract paper content from HTML, routing to the best adapter.

    Args:
        html: Raw HTML content.
        url: The URL the HTML was fetched from (used for publisher detection).

    Returns:
        Dict with title, authors, abstract, full_text, figures, references.
    """
    # Try publisher-specific adapters first
    for adapter in _ADAPTERS:
        if adapter.can_handle(url):
            logger.info("Using %s adapter for %s", adapter.__name__, url)
            return adapter.extract(html, url)

    # Fallback to generic
    logger.info("Using generic adapter for %s", url)
    return generic.extract(html, url)
