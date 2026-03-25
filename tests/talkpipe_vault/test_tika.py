"""Unit tests for the tika_extract function."""

from pathlib import Path

import pytest

# Skip all tests in this module if tika is not installed
pytest.importorskip("tika")

from talkpipe import compile
from talkpipe.data.extraction import ExtractionResult

from talkpipe_vault.tika import tika_extract

# Get the path to sample documents
SAMPLE_DOCS_DIR = Path(__file__).parent.parent / "sampledocs"
PDF_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.pdf")
DOCX_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.docx")
HTML_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.html")
FILES = [PDF_FILE, DOCX_FILE, HTML_FILE]


class TestTikaExtract:
    """Test suite for tika_extract function."""

    def test_extract_on_different_types(self):
        """Test that readFile extracts content from various document types using tika."""
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
        results = list(tika_extract("/nonexistent/path/to/file.pdf"))
        assert results == []

    def test_tika_extract_directly(self):
        """Test tika_extract function directly on sample document files."""
        for file_path in FILES:
            results = list(tika_extract(file_path))
            assert len(results) == 1
            assert isinstance(results[0], ExtractionResult)
            assert len(results[0].content) > 0
            assert results[0].source
            assert results[0].id
            assert results[0].title

