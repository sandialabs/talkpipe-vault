"""Text extraction segments for talkpipe_vault.

This module provides segments for extracting text from documents using various
extraction backends (Tika, Docling, etc.).
"""

import logging
from pathlib import Path
from typing import Annotated, Any

from talkpipe.chatterlang import register_segment
from talkpipe.pipe.basic import AbstractFieldSegment

logger = logging.getLogger(__name__)


@register_segment("tikaToText")
class TikaToText(AbstractFieldSegment):
    """
    Segment that extracts text content from documents using Apache Tika.

    Converts PDF, DOCX, PPTX, HTML, and other formats to plain text.
    Native support for plain text (`.txt`) files without Tika overhead.
    Graceful error handling: logs warnings and skips files that fail to convert.
    Works as a field segment with `field` and `set_as` parameters.

    Expects input items containing a file path (either as the full item or in
    a specified field). The file path is used to extract text using Tika.

    Emits dicts containing:
        - "content": str - Extracted text content
        - "source": str - Source file path
        - "id": str - Document identifier (file path)
        - "title": str - Source file name
    """
    def __init__(
        self,
        field: Annotated[str, "The field to extract. If none, use full item."] = None,
        set_as: Annotated[str, "The field to set/append the result as."] = None,
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = False):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)

    def process_value(self, value: str) -> dict[str, Any] | None:
        """Extract text from file using Tika."""
        try:
            from talkpipe_vault.tika import tika_extract
            
            path = Path(value)
            results = list(tika_extract(path))
            
            if not results:
                logger.warning(f"TikaToText: No content extracted from '{value}'")
                return None
            
            # Return the first result as a dict
            result = results[0]
            return {
                "content": result.content,
                "source": result.source,
                "id": result.id,
                "title": result.title
            }
        except ImportError:
            logger.error("Tika is not installed. Install it with: pip install talkpipe-vault[tika]")
            return None
        except Exception as e:
            logger.warning(f"TikaToText: Failed to extract text from '{value}': {e}")
            return None


@register_segment("doclingToText")
class DoclingToText(AbstractFieldSegment):
    """
    Segment that extracts text content from documents using the Docling library.

    Converts PDF, DOCX, PPTX, HTML, and other formats to markdown text.
    Native support for plain text (`.txt`) files without Docling overhead.
    Graceful error handling: logs warnings and skips files that fail to convert.
    Works as a field segment with `field` and `set_as` parameters.

    Expects input items containing a file path (either as the full item or in
    a specified field). The file path is used to extract text using Docling.

    Emits dicts containing:
        - "content": str - Extracted text content (markdown)
        - "source": str - Source file path
        - "id": str - Document identifier (file path)
        - "title": str - Source file name
    """
    def __init__(
        self,
        field: Annotated[str, "The field to extract. If none, use full item."] = None,
        set_as: Annotated[str, "The field to set/append the result as."] = None,
        multi_emit: Annotated[bool, "Whether this class potentially emits multiple results per item."
                                    "Should be set by the subclass constructor call or the field_segment decorator, not by the user."] = False):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)

    def process_value(self, value: str) -> dict[str, Any] | None:
        """Extract text from file using Docling."""
        try:
            from talkpipe_vault.docling import docling_extract
            
            path = Path(value)
            results = list(docling_extract(path))
            
            if not results:
                logger.warning(f"DoclingToText: No content extracted from '{value}'")
                return None
            
            # Return the first result as a dict
            result = results[0]
            return {
                "content": result.content,
                "source": result.source,
                "id": result.id,
                "title": result.title
            }
        except ImportError:
            logger.error("Docling is not installed. Install it with: pip install talkpipe-vault[docling]")
            return None
        except Exception as e:
            logger.warning(f"DoclingToText: Failed to extract text from '{value}': {e}")
            return None

