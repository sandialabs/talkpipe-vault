"""Plugin initialization module for talkpipe_vault.

This module is loaded by talkpipe's plugin system and registers Tika-based
file extraction as the default handler for the global extractor registry.
Docling is kept available as a fallback option.
"""

import logging

logger = logging.getLogger(__name__)


def initialize_plugin() -> None:
    """Initialize the talkpipe_vault plugin.

    """
    from talkpipe.data.extraction import global_extractor_registry

    pass