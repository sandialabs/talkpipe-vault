"""Unit tests for text extraction segments (tikaToText and doclingToText)."""

from pathlib import Path

import pytest
from talkpipe import compile

from talkpipe_vault.segments import DoclingToText, TikaToText

# Get the path to sample documents
SAMPLE_DOCS_DIR = Path(__file__).parent.parent / "sampledocs"
PDF_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.pdf")
DOCX_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.docx")
HTML_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.html")
FILES = [PDF_FILE, DOCX_FILE, HTML_FILE]


class TestTikaToText:
    """Test suite for TikaToText segment."""

    @pytest.fixture(autouse=True)
    def skip_if_tika_not_installed(self):
        """Skip all tests if tika is not installed."""
        pytest.importorskip("tika")

    def test_segment_is_registered(self):
        """Test that the segment is properly registered with TalkPipe."""
        script = compile("| tikaToText")
        assert script is not None

    def test_segment_callable(self):
        """Test that TikaToText class is instantiable."""
        segment = TikaToText()
        assert segment is not None
        assert hasattr(segment, "process_value")

    def test_extract_text_from_pdf(self):
        """Test extracting text from PDF file."""
        segment = TikaToText()
        result = segment.process_value(PDF_FILE)
        
        assert result is not None
        assert isinstance(result, dict)
        assert "content" in result
        assert "source" in result
        assert "id" in result
        assert "title" in result
        assert len(result["content"]) > 0
        assert result["source"] == str(Path(PDF_FILE).resolve())
        assert result["title"] == Path(PDF_FILE).name

    def test_extract_text_from_docx(self):
        """Test extracting text from DOCX file."""
        segment = TikaToText()
        result = segment.process_value(DOCX_FILE)
        
        assert result is not None
        assert isinstance(result, dict)
        assert len(result["content"]) > 0

    def test_extract_text_from_html(self):
        """Test extracting text from HTML file."""
        segment = TikaToText()
        result = segment.process_value(HTML_FILE)
        
        assert result is not None
        assert isinstance(result, dict)
        assert len(result["content"]) > 0

    def test_invalid_file_path(self):
        """Test that processing an invalid file path returns None."""
        segment = TikaToText()
        result = segment.process_value("/nonexistent/path/to/file.pdf")
        assert result is None

    def test_with_field_parameter(self):
        """Test TikaToText with field parameter."""
        segment = TikaToText(field="file_path")
        assert segment is not None

    def test_with_set_as_parameter(self):
        """Test TikaToText with set_as parameter."""
        segment = TikaToText(set_as="extracted_text")
        assert segment is not None


class TestDoclingToText:
    """Test suite for DoclingToText segment."""

    @pytest.fixture(autouse=True)
    def skip_if_docling_not_installed(self):
        """Skip all tests if docling is not installed."""
        pytest.importorskip("docling")

    def test_segment_is_registered(self):
        """Test that the segment is properly registered with TalkPipe."""
        script = compile("| doclingToText")
        assert script is not None

    def test_segment_callable(self):
        """Test that DoclingToText class is instantiable."""
        segment = DoclingToText()
        assert segment is not None
        assert hasattr(segment, "process_value")

    def test_extract_text_from_pdf(self):
        """Test extracting text from PDF file."""
        segment = DoclingToText()
        result = segment.process_value(PDF_FILE)
        
        assert result is not None
        assert isinstance(result, dict)
        assert "content" in result
        assert "source" in result
        assert "id" in result
        assert "title" in result
        assert len(result["content"]) > 0
        assert result["source"] == str(Path(PDF_FILE).resolve())
        assert result["title"] == Path(PDF_FILE).name

    def test_extract_text_from_docx(self):
        """Test extracting text from DOCX file."""
        segment = DoclingToText()
        result = segment.process_value(DOCX_FILE)
        
        assert result is not None
        assert isinstance(result, dict)
        assert len(result["content"]) > 0

    def test_extract_text_from_html(self):
        """Test extracting text from HTML file."""
        segment = DoclingToText()
        result = segment.process_value(HTML_FILE)
        
        assert result is not None
        assert isinstance(result, dict)
        assert len(result["content"]) > 0

    def test_invalid_file_path(self):
        """Test that processing an invalid file path returns None."""
        segment = DoclingToText()
        result = segment.process_value("/nonexistent/path/to/file.pdf")
        assert result is None

    def test_with_field_parameter(self):
        """Test DoclingToText with field parameter."""
        segment = DoclingToText(field="file_path")
        assert segment is not None

    def test_with_set_as_parameter(self):
        """Test DoclingToText with set_as parameter."""
        segment = DoclingToText(set_as="extracted_text")
        assert segment is not None

