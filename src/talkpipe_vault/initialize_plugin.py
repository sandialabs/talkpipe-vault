"""Plugin initialization module for talkpipe_vault.

This module is loaded by talkpipe's plugin system and registers Docling-based
file extraction as the default handler for the global extractor registry.
"""

import logging

logger = logging.getLogger(__name__)


def initialize_plugin() -> None:
    """Initialize the talkpipe_vault plugin.

    Registers the docling_extract function as the default extractor in
    talkpipe's global extractor registry if docling is installed, enabling
    extraction from 50+ file formats including PDF, DOCX, and source code files.
    """
    try:
        from talkpipe.data.extraction import global_extractor_registry
        from talkpipe_vault.docling import docling_extract

        logger.info("Registering docling_extract as default file extractor")
        global_extractor_registry.register_default(docling_extract)
    except ImportError:
        logger.warning(
            "docling is not installed. Install it with: pip install talkpipe-vault[docling]"
        )
