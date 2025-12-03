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
    list_into_vector_db,
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
            overwrite=True,
        )

        input_data = [{"path": PDF_FILE, "event": "created"}]
        results = list(segment(input_data))

        assert len(results) > 0

    def test_build_vector_db_with_multiple_documents(self):
        """Test building vector DB with multiple documents."""
        segment = build_vector_db_from_paths(
            vectordb_path="tmp://test_multi_doc",
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
            overwrite=True,
        )

        input_data = [{"path": PDF_FILE, "event": "created"}]
        list(segment(input_data))

        # Verify tables were created
        db = LanceDBDocumentStore(path=vectordb_path, table_name="full_documents")
        assert db.count() == 1
        doc = db.get_document(PDF_FILE)
        assert doc["path"] == PDF_FILE

        # Verify shingled_chunks table was created
        # Note: shingled_chunks uses composite ID format: "first_paragraph-last_paragraph-path"
        db = LanceDBDocumentStore(path=vectordb_path, table_name="shingled_chunks")
        assert db.count() == 1
        # For a short document with one chunk, the ID is "0-0-{path}"
        shingle_id = f"0-0-{PDF_FILE}"
        doc = db.get_document(shingle_id)
        assert doc is not None
        assert doc["id"] == shingle_id


class TestWatchIntoVectorDb:
    """Tests for the watch_into_vector_db source."""

    def test_source_is_registered(self):
        """Test that the source is properly registered with TalkPipe."""
        script = compile(
            "INPUT FROM watchIntoVectorDB["
            "source_path='/tmp', "
            "vectordb_path='memory://', "
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


class TestListIntoVectorDb:
    """Tests for the list_into_vector_db source."""

    def test_source_is_registered(self):
        """Test that the source is properly registered with TalkPipe."""
        script = compile(
            "INPUT FROM listIntoVectorDB["
            "source_path='/tmp', "
            "vectordb_path='memory://'"
            "] | toList"
        )
        assert script is not None

    def test_source_callable(self):
        """Test that the source function is callable."""
        assert callable(list_into_vector_db)

    def test_list_and_process_directory_files(self):
        """Test that all files in a directory are listed and processed."""
        with tempfile.TemporaryDirectory() as source_dir:
            # Create test files
            test_file1 = Path(source_dir) / "document1.txt"
            test_file1.write_text("This is the first test document for the vector database.")

            test_file2 = Path(source_dir) / "document2.txt"
            test_file2.write_text("This is the second test document for the vector database.")

            # Run the pipeline
            source = list_into_vector_db(
                source_pattern=source_dir,
                vectordb_path="tmp://test_list_files",
                overwrite=True,
            )
            results = list(source())

            # Verify we got results
            assert len(results) > 0

            # Verify both files were processed
            paths = [r.get("path", "") for r in results]
            assert any("document1.txt" in p for p in paths)
            assert any("document2.txt" in p for p in paths)

    def test_list_creates_vector_db_tables(self):
        """Test that listing files creates both vector DB tables."""
        from talkpipe.search.lancedb import LanceDBDocumentStore

        with tempfile.TemporaryDirectory() as source_dir:
            # Create a test file
            test_file = Path(source_dir) / "test_doc.txt"
            test_file.write_text("This is test content for verifying table creation.")

            vectordb_path = "tmp://test_list_creates_tables"

            # Run the pipeline
            source = list_into_vector_db(
                source_pattern=source_dir,
                vectordb_path=vectordb_path,
                overwrite=True,
            )
            list(source())

            # Verify full_documents table was created
            db = LanceDBDocumentStore(path=vectordb_path, table_name="full_documents")
            assert db.count() >= 1

            # Verify shingled_chunks table was created
            db = LanceDBDocumentStore(path=vectordb_path, table_name="shingled_chunks")
            assert db.count() >= 1

    def test_list_with_subdirectories(self):
        """Test that files in subdirectories are processed with glob patterns."""
        with tempfile.TemporaryDirectory() as source_dir:
            # Create files in root
            root_file = Path(source_dir) / "root.txt"
            root_file.write_text("Root level document.")

            # Create subdirectory with file
            subdir = Path(source_dir) / "subdir"
            subdir.mkdir()
            sub_file = subdir / "nested.txt"
            sub_file.write_text("Nested document in subdirectory.")

            # Run the pipeline with glob pattern for recursive matching
            source = list_into_vector_db(
                source_pattern=f"{source_dir}/**/*.txt",
                vectordb_path="tmp://test_list_subdirs",
                overwrite=True,
            )
            results = list(source())

            # Verify both files were processed
            assert len(results) > 0
            paths = [r.get("path", "") for r in results]
            assert any("root.txt" in p for p in paths)
            assert any("nested.txt" in p for p in paths)

    def test_list_with_sample_documents(self):
        """Test processing actual sample documents (PDF, DOCX, HTML)."""
        # Run the pipeline on the sample docs directory
        source = list_into_vector_db(
            source_pattern=str(SAMPLE_DOCS_DIR),
            vectordb_path="tmp://test_list_samples",
            overwrite=True,
        )
        results = list(source())

        # Verify we got results
        assert len(results) > 0

        # Verify sample documents were processed
        paths = [r.get("path", "") for r in results]
        assert any("SampleDocument.pdf" in p for p in paths)
        assert any("SampleDocument.docx" in p for p in paths)
        assert any("SampleDocument.html" in p for p in paths)
