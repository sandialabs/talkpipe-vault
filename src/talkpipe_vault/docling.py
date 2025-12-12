"""Docling-based file extraction for talkpipe.

This module provides a file extractor function that uses Docling to convert
documents (PDF, DOCX, HTML, etc.) to markdown. When the talkpipe_vault plugin
is loaded, this extractor is registered as the default handler in talkpipe's
global extractor registry, providing support for 50+ document formats.
"""

import logging
from pathlib import Path
from typing import Iterator, Union

from docling.document_converter import DocumentConverter
from talkpipe.data.extraction import ExtractionResult

logger = logging.getLogger(__name__)

# Lazy-initialized converter instance
_converter: DocumentConverter | None = None


def _get_converter() -> DocumentConverter:
    """Get or create the shared DocumentConverter instance."""
    global _converter
    if _converter is None:
        _converter = DocumentConverter()
    return _converter


def docling_extract(file_path: Union[str, Path]) -> Iterator[ExtractionResult]:
    """
    Extract text from document files using Docling.

    Converts document formats (PDF, DOCX, HTML, etc.) via Docling and exports
    as markdown. This function is registered as the default extractor in
    talkpipe's global extractor registry, handling file types not covered by
    the built-in text extractors.

    Args:
        file_path: Path to the document file to extract text from.

    Yields:
        ExtractionResult objects containing extracted content, source path, id, and title.
        Yields nothing if extraction fails.
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path
    source_str = str(path.resolve())

    try:
        logger.debug(f"Converting document with Docling: {path}")
        converter = _get_converter()
        result = converter.convert(str(path))
        content = result.document.export_to_markdown()
        yield ExtractionResult(
            content=content,
            source=source_str,
            id=source_str,
            title=path.name
        )
    except Exception as e:
        logger.warning(f"Failed to convert '{file_path}': {e}")
