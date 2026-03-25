"""Unit tests for the docling_extract function."""

from pathlib import Path

import pytest

# Skip all tests in this module if docling is not installed
pytest.importorskip("docling")

from talkpipe import compile
from talkpipe.data.extraction import ExtractionResult

from talkpipe_vault.docling import docling_extract

# Get the path to sample documents
SAMPLE_DOCS_DIR = Path(__file__).parent.parent / "sampledocs"
PDF_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.pdf")
DOCX_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.docx")
HTML_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.html")
FILES = [PDF_FILE, DOCX_FILE, HTML_FILE]


class TestDoclingExtract:
    """Test suite for docling_extract function."""

    def test_extract_on_different_types(self):
        """Test that readFile extracts content from various document types using docling."""
        pipeline = compile(""" | readFile """)

        results = list(pipeline(FILES))

        for result in results:
            assert isinstance(result, ExtractionResult)
            assert len(result.content) > 0
            assert result.source
            assert result.id
            assert result.title

    def test_invalid_file_path(self):
        """Test that processing an invalid file path yields nothing (skip behavior)."""
        results = list(docling_extract("/nonexistent/path/to/file.pdf"))
        assert results == []

    def test_docling_extract_directly(self):
        """Test docling_extract function directly on sample document files."""
        for file_path in FILES:
            results = list(docling_extract(file_path))
            assert len(results) == 1
            assert isinstance(results[0], ExtractionResult)
            assert len(results[0].content) > 0
            assert results[0].source
            assert results[0].id
            assert results[0].title

