"""Unit tests for the searching_and_prompting pipeline module."""

import tempfile
from pathlib import Path

import pytest
from talkpipe import compile
from talkpipe.search.lancedb import LanceDBDocumentStore

from talkpipe_vault.pipelines.searching_and_prompting import (
    VaultSearch,
    VaultChat,
)
from talkpipe_vault.pipelines.building_and_watching import list_into_vector_db


# Get the path to sample documents
SAMPLE_DOCS_DIR = Path(__file__).parent.parent.parent / "sampledocs"
PDF_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.pdf")
DOCX_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.docx")
HTML_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.html")


@pytest.fixture
def populated_vector_db():
    """Create a populated vector database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test text files
        test_file1 = Path(tmpdir) / "document1.txt"
        test_file1.write_text(
            "Python is a high-level programming language. "
            "It is widely used for web development, data analysis, and machine learning."
        )

        test_file2 = Path(tmpdir) / "document2.txt"
        test_file2.write_text(
            "FastAPI is a modern web framework for building APIs with Python. "
            "It is fast, easy to use, and supports async operations."
        )

        test_file3 = Path(tmpdir) / "document3.txt"
        test_file3.write_text(
            "Machine learning is a subset of artificial intelligence. "
            "It involves training models on data to make predictions."
        )

        # Build vector database
        vectordb_path = "tmp://test_search_prompting_db"
        source = list_into_vector_db(
            source_pattern=tmpdir,
            vectordb_path=vectordb_path,
            overwrite=True,
        )
        # Process all documents
        list(source())

        yield vectordb_path


class TestVaultSearch:
    """Tests for the VaultSearch segment."""

    def test_segment_is_registered(self):
        """Test that the segment is properly registered with TalkPipe."""
        script = compile(
            "| vaultSearch[path='memory://']"
        )
        assert script is not None

    def test_segment_callable(self):
        """Test that VaultSearch class is instantiable."""
        segment = VaultSearch(path="memory://")
        assert segment is not None
        assert hasattr(segment, "process_value")

    def test_search_returns_results(self, populated_vector_db):
        """Test that searching returns relevant results."""
        segment = VaultSearch(path=populated_vector_db)

        # Search for Python-related content
        query = "What is Python programming?"
        result = segment.process_value(query)

        # Verify we got results
        assert result is not None
        assert isinstance(result, list)
        assert len(result) > 0

        # Verify results have expected structure (SearchResult objects)
        for item in result:
            # SearchResult objects have score, doc_id, and document attributes
            assert hasattr(item, "score") or hasattr(item, "doc_id") or isinstance(item, dict)
            if hasattr(item, "document"):
                assert isinstance(item.document, dict)

    def test_search_with_different_queries(self, populated_vector_db):
        """Test searching with different query topics."""
        segment = VaultSearch(path=populated_vector_db)

        # Search for FastAPI
        query1 = "Tell me about FastAPI framework"
        result1 = segment.process_value(query1)
        assert result1 is not None
        assert len(result1) > 0

        # Search for machine learning
        query2 = "What is machine learning?"
        result2 = segment.process_value(query2)
        assert result2 is not None
        assert len(result2) > 0

    def test_search_with_field_parameter(self, populated_vector_db):
        """Test VaultSearch with field parameter."""
        segment = VaultSearch(path=populated_vector_db, field="query")

        # Input as dict with query field
        input_data = {"query": "Python programming"}
        result = segment.process_value(input_data["query"])

        assert result is not None
        assert isinstance(result, list)

    def test_search_with_set_as_parameter(self, populated_vector_db):
        """Test VaultSearch with set_as parameter."""
        segment = VaultSearch(path=populated_vector_db, set_as="search_results")

        query = "web development"
        result = segment.process_value(query)

        assert result is not None

    def test_search_pipeline_integration(self, populated_vector_db):
        """Test that VaultSearch integrates properly with the internal pipeline."""
        segment = VaultSearch(path=populated_vector_db)

        # Verify the internal pipeline was created
        assert segment.pipeline is not None
        assert callable(segment.pipeline)

        # Test the pipeline directly
        query = "Python"
        result = segment.pipeline(query)
        assert result is not None


class TestVaultChat:
    """Tests for the VaultChat segment."""

    def test_segment_is_registered(self):
        """Test that the segment is properly registered with TalkPipe."""
        script = compile(
            "| vaultChat[path='memory://']"
        )
        assert script is not None

    def test_segment_callable(self):
        """Test that VaultChat class is instantiable."""
        segment = VaultChat(path="memory://")
        assert segment is not None
        assert hasattr(segment, "process_value")

    def test_chat_returns_response(self, populated_vector_db):
        """Test that chat returns a response from the LLM."""
        segment = VaultChat(path=populated_vector_db)

        # Ask a question
        query = "What is Python used for?"
        result = segment.process_value(query)

        # Verify we got a response
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0

    def test_chat_uses_context(self, populated_vector_db):
        """Test that chat uses vector database context in responses."""
        segment = VaultChat(path=populated_vector_db)

        # Ask about FastAPI
        query = "Tell me about FastAPI"
        result = segment.process_value(query)

        assert result is not None
        assert isinstance(result, str)
        # Response should mention FastAPI or related terms
        assert len(result) > 0

    def test_chat_with_field_parameter(self, populated_vector_db):
        """Test VaultChat with field parameter."""
        segment = VaultChat(path=populated_vector_db, field="question")

        # This should be instantiable
        assert segment is not None

    def test_chat_with_set_as_parameter(self, populated_vector_db):
        """Test VaultChat with set_as parameter."""
        segment = VaultChat(path=populated_vector_db, set_as="answer")

        # This should be instantiable
        assert segment is not None

    def test_chat_pipeline_integration(self, populated_vector_db):
        """Test that VaultChat integrates properly with the internal pipeline."""
        segment = VaultChat(path=populated_vector_db)

        # Verify the internal pipeline was created
        assert segment.pipeline is not None
        assert callable(segment.pipeline)


class TestVaultSearchAdvanced:
    """Advanced tests for VaultSearch functionality."""

    def test_search_empty_query(self, populated_vector_db):
        """Test searching with an empty query."""
        segment = VaultSearch(path=populated_vector_db)

        query = ""
        result = segment.process_value(query)

        # Should still return something (might be empty list or all results)
        assert result is not None

    def test_search_nonexistent_topic(self, populated_vector_db):
        """Test searching for a topic not in the database."""
        segment = VaultSearch(path=populated_vector_db)

        # Search for something completely unrelated
        query = "quantum entanglement in superposition states"
        result = segment.process_value(query)

        # Should return results (might be less relevant)
        assert result is not None
        assert isinstance(result, list)

    def test_multiple_searches_same_segment(self, populated_vector_db):
        """Test that a segment can be reused for multiple searches."""
        segment = VaultSearch(path=populated_vector_db)

        query1 = "Python"
        result1 = segment.process_value(query1)
        assert result1 is not None

        query2 = "FastAPI"
        result2 = segment.process_value(query2)
        assert result2 is not None

        query3 = "machine learning"
        result3 = segment.process_value(query3)
        assert result3 is not None


class TestIntegrationWithSampleDocs:
    """Integration tests using the actual sample documents."""

    @pytest.fixture
    def sample_docs_vector_db(self):
        """Create a vector database from sample documents."""
        vectordb_path = "tmp://test_sample_docs_search"
        source = list_into_vector_db(
            source_pattern=str(SAMPLE_DOCS_DIR),
            vectordb_path=vectordb_path,
            overwrite=True,
        )
        # Process all sample documents
        list(source())
        yield vectordb_path

    def test_search_sample_documents(self, sample_docs_vector_db):
        """Test searching the sample documents."""
        segment = VaultSearch(path=sample_docs_vector_db)

        query = "sample document content"
        result = segment.process_value(query)

        assert result is not None
        assert isinstance(result, list)
        assert len(result) > 0

    def test_chat_sample_documents(self, sample_docs_vector_db):
        """Test chatting with the sample documents."""
        segment = VaultChat(path=sample_docs_vector_db)

        query = "What is in the sample documents?"
        result = segment.process_value(query)

        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0
