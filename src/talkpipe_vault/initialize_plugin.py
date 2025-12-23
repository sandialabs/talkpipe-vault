"""Plugin initialization module for talkpipe_vault.

This module is loaded by talkpipe's plugin system and registers Tika-based
file extraction as the default handler for the global extractor registry.
Docling is kept available as a fallback option.
"""

import logging

logger = logging.getLogger(__name__)


def initialize_plugin() -> None:
    """Initialize the talkpipe_vault plugin.

    Registers the tika_extract function as the default extractor in
    talkpipe's global extractor registry if tika is installed, enabling
    extraction from 1000+ file formats including PDF, DOCX, and source code files.
    Falls back to docling_extract if tika is not available.
    """
    from talkpipe.data.extraction import global_extractor_registry
    
    # Try to register Tika as the default extractor
    try:
        from talkpipe_vault.tika import tika_extract
        
        logger.info("Registering tika_extract as default file extractor")
        global_extractor_registry.register_default(tika_extract)
    except ImportError:
        logger.warning(
            "tika is not installed. Install it with: pip install talkpipe-vault[tika]. "
            "Falling back to docling if available."
        )
        # Fall back to docling if tika is not available
        try:
            from talkpipe_vault.docling import docling_extract
            
            logger.info("Registering docling_extract as default file extractor (fallback)")
            global_extractor_registry.register_default(docling_extract)
        except ImportError:
            logger.warning(
                "Neither tika nor docling is installed. Install one with: "
                "pip install talkpipe-vault[tika] or pip install talkpipe-vault[docling]"
            )
