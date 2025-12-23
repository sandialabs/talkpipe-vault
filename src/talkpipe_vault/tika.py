"""Tika-based file extraction for talkpipe.

This module provides a file extractor function that uses Apache Tika to extract
text from documents (PDF, DOCX, HTML, etc.). When the talkpipe_vault plugin is
loaded, this extractor is registered as the default handler in talkpipe's global
extractor registry, providing support for 1000+ document formats.
"""

import logging
from pathlib import Path
from typing import Iterator, Union

from tika import parser
from talkpipe.data.extraction import ExtractionResult

logger = logging.getLogger(__name__)


def tika_extract(file_path: Union[str, Path]) -> Iterator[ExtractionResult]:
    """
    Extract text from document files using Apache Tika.

    Converts document formats (PDF, DOCX, HTML, etc.) via Tika and extracts
    plain text. This function is registered as the default extractor in
    talkpipe's global extractor registry, handling file types not covered by
    the built-in text extractors.

    Tika runs in server mode in the background, automatically managed by the
    tika-python library.

    Args:
        file_path: Path to the document file to extract text from.

    Yields:
        ExtractionResult objects containing extracted content, source path, id, and title.
        Yields nothing if extraction fails.
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path
    source_str = str(path.resolve())

    try:
        logger.debug(f"Extracting text with Tika: {path}")
        parsed = parser.from_file(str(path))
        content = parsed.get('content', '')
        
        if not content or not content.strip():
            logger.warning(f"Tika extracted empty content from '{file_path}'")
            return
        
        yield ExtractionResult(
            content=content,
            source=source_str,
            id=source_str,
            title=path.name
        )
    except Exception as e:
        logger.warning(f"Failed to extract text from '{file_path}' with Tika: {e}")

