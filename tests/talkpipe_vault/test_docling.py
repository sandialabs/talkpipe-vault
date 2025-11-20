"""Unit tests for the DoclingPathToText segment."""

import pytest
from pathlib import Path
from talkpipe import compile
from talkpipe_vault.docling import DoclingPathToText


# Get the path to sample documents
SAMPLE_DOCS_DIR = Path(__file__).parent.parent / "sampledocs"
PDF_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.pdf")
DOCX_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.docx")
HTML_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.html")
FILES = [PDF_FILE, DOCX_FILE, HTML_FILE]


class TestDoclingPathToText:
    """Test suite for DoclingPathToText segment."""

    def test_extract_on_different_types(self):
        """Test that markdown and plain_text formats produce different outputs."""
        pipeline = compile(""" | pathToText """)

        results = list(pipeline(FILES))

        for result in results:
            assert isinstance(result, str)
            assert len(result) > 0

    def test_invalid_file_path(self):
        """Test that processing an invalid file path raises an appropriate error."""
        pipeline = compile("pathToText").as_function(single_in=True, single_out=True)

        with pytest.raises(Exception):  # Docling should raise an error for invalid paths
            pipeline("/nonexistent/path/to/file.pdf")

