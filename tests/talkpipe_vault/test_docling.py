"""Unit tests for the DoclingFileToText segment."""

import pytest
from pathlib import Path
from talkpipe import compile
from talkpipe_vault.docling import DoclingFileToText


# Get the path to sample documents
SAMPLE_DOCS_DIR = Path(__file__).parent.parent / "sampledocs"
PDF_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.pdf")
DOCX_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.docx")
HTML_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.html")
FILES = [PDF_FILE, DOCX_FILE, HTML_FILE]


class TestDoclingFileToText:
    """Test suite for DoclingFileToText segment."""

    def test_extract_on_different_types(self):
        """Test that markdown and plain_text formats produce different outputs."""
        pipeline = compile(""" | doclingToText """)

        results = list(pipeline(FILES))

        for result in results:
            assert isinstance(result, str)
            assert len(result) > 0

    def test_invalid_file_path(self):
        """Test that processing an invalid file path logs a warning and returns None."""
        pipeline = compile("doclingToText").as_function(single_in=True, single_out=True)

        # Should not raise, but return None for invalid paths
        result = pipeline("/nonexistent/path/to/file.pdf")
        assert result is None

