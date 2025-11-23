"""Unit tests for the building_and_watching pipeline module."""

import tempfile
import time
from pathlib import Path
from threading import Thread

import pytest
from talkpipe import compile

from talkpipe_vault.pipelines.building_and_watching import (
    build_vector_db_from_paths,
    watch_into_vector_db,
)


# Get the path to sample documents
SAMPLE_DOCS_DIR = Path(__file__).parent.parent.parent / "sampledocs"
PDF_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.pdf")
DOCX_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.docx")
HTML_FILE = str(SAMPLE_DOCS_DIR / "SampleDocument.html")

# Embedding configuration
EMBEDDING_MODEL = "mxbai-embed-large:latest"
EMBEDDING_SOURCE = "ollama"


class TestBuildVectorDbFromPaths:
    """Tests for the build_vector_db_from_paths segment."""

    def test_segment_is_registered(self):
        """Test that the segment is properly registered with TalkPipe."""
        script = compile(
            "| buildVectorDBFromPaths["
            "vectordb_path='memory://', "
            f"embedding_model='{EMBEDDING_MODEL}', "
            f"embedding_source='{EMBEDDING_SOURCE}']"
        )
        assert script is not None

    def test_segment_callable(self):
        """Test that the segment function is callable."""
        assert callable(build_vector_db_from_paths)

    def test_build_vector_db_with_single_document(self):
        """Test building vector DB with a single document."""
        segment = build_vector_db_from_paths(
            vectordb_path="tmp://test_single_doc",
            embedding_model=EMBEDDING_MODEL,
            embedding_source=EMBEDDING_SOURCE,
            overwrite=True,
        )

        input_data = [{"path": PDF_FILE, "event": "created"}]
        results = list(segment(input_data))

        assert len(results) > 0

    def test_build_vector_db_with_multiple_documents(self):
        """Test building vector DB with multiple documents."""
        segment = build_vector_db_from_paths(
            vectordb_path="tmp://test_multi_doc",
            embedding_model=EMBEDDING_MODEL,
            embedding_source=EMBEDDING_SOURCE,
            overwrite=True,
        )

        input_data = [
            {"path": PDF_FILE, "event": "created"},
            {"path": DOCX_FILE, "event": "created"},
            {"path": HTML_FILE, "event": "created"},
        ]
        results = list(segment(input_data))

        assert len(results) > 0

    def test_build_vector_db_creates_tables(self):
        """Test that both full_documents and shingled_chunks tables are created."""
        from talkpipe.search.lancedb import LanceDBDocumentStore
        import lancedb

        vectordb_path = "tmp://test_creates_tables"

        segment = build_vector_db_from_paths(
            vectordb_path=vectordb_path,
            embedding_model=EMBEDDING_MODEL,
            embedding_source=EMBEDDING_SOURCE,
            overwrite=True,
        )

        input_data = [{"path": PDF_FILE, "event": "created"}]
        list(segment(input_data))

        # Verify tables were created
        db = LanceDBDocumentStore(path=vectordb_path, table_name="full_documents")
        assert db.count() == 1
        doc = db.get_document(PDF_FILE)
        assert doc["path"] == PDF_FILE
        db = LanceDBDocumentStore(path=vectordb_path, table_name="shingled_chunks")
        assert db.count() == 1
        doc = db.get_document(PDF_FILE)
        assert doc["path"] == PDF_FILE


class TestWatchIntoVectorDb:
    """Tests for the watch_into_vector_db source."""

    def test_source_is_registered(self):
        """Test that the source is properly registered with TalkPipe."""
        script = compile(
            "INPUT FROM watchIntoVectorDB["
            "source_path='/tmp', "
            "vectordb_path='memory://', "
            f"embedding_model='{EMBEDDING_MODEL}', "
            f"embedding_source='{EMBEDDING_SOURCE}', "
            "max_events=1"
            "] | toList"
        )
        assert script is not None

    def test_source_callable(self):
        """Test that the source function is callable."""
        assert callable(watch_into_vector_db)

    def test_watch_and_process_file_creation(self):
        """Test that file creation triggers vector DB processing."""
        with tempfile.TemporaryDirectory() as watch_dir:
            results = []

            def run_pipeline():
                source = watch_into_vector_db(
                    source_path=watch_dir,
                    vectordb_path="tmp://test_watch_creation",
                    embedding_model=EMBEDDING_MODEL,
                    embedding_source=EMBEDDING_SOURCE,
                    patterns=["*.txt"],
                    max_events=1,
                    overwrite=True,
                )
                for item in source():
                    results.append(item)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create a text file
            test_file = Path(watch_dir) / "test.txt"
            test_file.write_text("This is test content for the vector database.")

            # Wait for thread to complete
            thread.join(timeout=30.0)

            assert len(results) > 0

    def test_watch_with_pattern_filtering(self):
        """Test that pattern filtering works during watch."""
        with tempfile.TemporaryDirectory() as watch_dir:
            results = []

            def run_pipeline():
                source = watch_into_vector_db(
                    source_path=watch_dir,
                    vectordb_path="tmp://test_watch_filtering",
                    embedding_model=EMBEDDING_MODEL,
                    embedding_source=EMBEDDING_SOURCE,
                    patterns=["*.txt"],
                    max_events=4,
                    overwrite=True,
                )
                for item in source():
                    results.append(item)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create a .md file (should be ignored)
            md_file = Path(watch_dir) / "ignored.md"
            md_file.write_text("This should be ignored.")
            time.sleep(0.5)

            # Create two .txt files (should be processed)
            txt_file1 = Path(watch_dir) / "test1.txt"
            txt_file1.write_text("First text file content.")
            time.sleep(0.5)

            txt_file2 = Path(watch_dir) / "test2.txt"
            txt_file2.write_text("Second text file content.")

            # Wait for thread to complete
            thread.join(timeout=30.0)

            # Debug: print what we got
            print(f"\n=== DEBUG: Got {len(results)} results ===")
            for i, r in enumerate(results):
                print(f"  [{i}] path={r.get('path', 'NO PATH')} keys={list(r.keys())}")

            # Verify .txt files were processed, .md was not
            paths = [r.get("path", "") for r in results]
            assert any("test1.txt" in p for p in paths)
            assert any("test2.txt" in p for p in paths)
            assert not any("ignored.md" in p for p in paths)
