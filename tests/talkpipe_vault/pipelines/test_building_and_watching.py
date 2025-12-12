"""Unit tests for the building_and_watching pipeline module."""

import os
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


class TestBuildVectorDbFromPaths:
    """Tests for the build_vector_db_from_paths segment."""

    def test_segment_is_registered(self):
        """Test that the segment is properly registered with TalkPipe."""
        script = compile(
            "| buildVectorDBFromPaths["
            "vault_path='/tmp/test_vault']"
        )
        assert script is not None

    def test_segment_callable(self):
        """Test that the segment function is callable."""
        assert callable(build_vector_db_from_paths)

    def test_build_vector_db_with_single_document(self):
        """Test building vector DB with a single document."""
        with tempfile.TemporaryDirectory() as vault_path:
            segment = build_vector_db_from_paths(
                vault_path=vault_path,
                overwrite=True,
            )

            input_data = [{"path": PDF_FILE, "event": "created"}]
            results = list(segment(input_data))

            assert len(results) > 0

    def test_build_vector_db_with_multiple_documents(self):
        """Test building vector DB with multiple documents."""
        with tempfile.TemporaryDirectory() as vault_path:
            segment = build_vector_db_from_paths(
                vault_path=vault_path,
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

        with tempfile.TemporaryDirectory() as vault_path:
            vectordb_path = os.path.join(vault_path, "vector_vault")

            segment = build_vector_db_from_paths(
                vault_path=vault_path,
                overwrite=True,
            )

            input_data = [{"path": PDF_FILE, "event": "created"}]
            list(segment(input_data))

            # Verify tables were created
            db = LanceDBDocumentStore(path=vectordb_path, table_name="full_documents")
            assert db.count() == 1
            doc = db.get_document(PDF_FILE)
            # The document should have 'source' field instead of 'path' now
            assert "source" in doc or "id" in doc

            # Verify shingled_chunks table was created
            # Note: shingled_chunks uses composite ID format: "first_paragraph-last_paragraph-source"
            db = LanceDBDocumentStore(path=vectordb_path, table_name="shingled_chunks")
            assert db.count() == 1
            # For a short document with one chunk, the ID is "0-0-{source}"
            shingle_id = f"0-0-{PDF_FILE}"
            doc = db.get_document(shingle_id)
            assert doc is not None
            # The document should exist and have shingle_id stored
            assert "shingle_id" in doc or "_id" in doc

    def test_build_vector_db_creates_whoosh_index(self):
        """Test that Whoosh full-text index is created."""
        from talkpipe.search.whoosh import WhooshFullTextIndex

        with tempfile.TemporaryDirectory() as vault_path:
            whoosh_index_path = os.path.join(vault_path, "fulltext_vault")

            segment = build_vector_db_from_paths(
                vault_path=vault_path,
                overwrite=True,
            )

            input_data = [{"path": PDF_FILE, "event": "created"}]
            list(segment(input_data))

            # Verify Whoosh index was created
            idx = WhooshFullTextIndex(whoosh_index_path)

            # Verify we can search the index for content from the PDF
            # The sample PDF contains "Heading" and "text"
            results = idx.text_search("Heading", limit=10)
            assert len(results) > 0
            # Verify result has expected attributes
            assert hasattr(results[0], 'doc_id') or hasattr(results[0], 'document')


class TestWatchIntoVectorDb:
    """Tests for the watch_into_vector_db source."""

    def test_source_is_registered(self):
        """Test that the source is properly registered with TalkPipe."""
        script = compile(
            "INPUT FROM watchIntoVectorDB["
            "source_path='/tmp', "
            "vault_path='/tmp/test_vault', "
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
            with tempfile.TemporaryDirectory() as vault_path:
                results = []

                def run_pipeline():
                    source = watch_into_vector_db(
                        source_path=watch_dir,
                        vault_path=vault_path,
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
            with tempfile.TemporaryDirectory() as vault_path:
                results = []

                def run_pipeline():
                    source = watch_into_vector_db(
                        source_path=watch_dir,
                        vault_path=vault_path,
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
                    print(f"  [{i}] source={r.get('source', 'NO SOURCE')} keys={list(r.keys())}")

                # Verify .txt files were processed, .md was not
                sources = [r.get("source", "") for r in results]
                assert any("test1.txt" in p for p in sources)
                assert any("test2.txt" in p for p in sources)
                assert not any("ignored.md" in p for p in sources)


class TestListIntoVectorDb:
    """Tests for the list_into_vector_db source."""

    def test_source_is_registered(self):
        """Test that the source is properly registered with TalkPipe."""
        script = compile(
            "INPUT FROM listIntoVectorDB["
            "source_pattern='/tmp', "
            "vault_path='/tmp/test_vault'"
            "] | toList"
        )
        assert script is not None

    def test_source_callable(self):
        """Test that the source function is callable."""
        assert callable(list_into_vector_db)

    def test_list_and_process_directory_files(self):
        """Test that all files in a directory are listed and processed."""
        with tempfile.TemporaryDirectory() as source_dir:
            with tempfile.TemporaryDirectory() as vault_path:
                # Create test files
                test_file1 = Path(source_dir) / "document1.txt"
                test_file1.write_text("This is the first test document for the vector database.")

                test_file2 = Path(source_dir) / "document2.txt"
                test_file2.write_text("This is the second test document for the vector database.")

                # Run the pipeline
                source = list_into_vector_db(
                    source_pattern=source_dir,
                    vault_path=vault_path,
                    overwrite=True,
                )
                results = list(source())

                # Verify we got results
                assert len(results) > 0

                # Verify both files were processed
                sources = [r.get("source", "") for r in results]
                assert any("document1.txt" in p for p in sources)
                assert any("document2.txt" in p for p in sources)

    def test_list_creates_vector_db_tables(self):
        """Test that listing files creates both vector DB tables."""
        from talkpipe.search.lancedb import LanceDBDocumentStore

        with tempfile.TemporaryDirectory() as source_dir:
            with tempfile.TemporaryDirectory() as vault_path:
                # Create a test file
                test_file = Path(source_dir) / "test_doc.txt"
                test_file.write_text("This is test content for verifying table creation.")

                vectordb_path = os.path.join(vault_path, "vector_vault")

                # Run the pipeline
                source = list_into_vector_db(
                    source_pattern=source_dir,
                    vault_path=vault_path,
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
            with tempfile.TemporaryDirectory() as vault_path:
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
                    vault_path=vault_path,
                    overwrite=True,
                )
                results = list(source())

                # Verify both files were processed
                assert len(results) > 0
                sources = [r.get("source", "") for r in results]
                assert any("root.txt" in p for p in sources)
                assert any("nested.txt" in p for p in sources)

    def test_list_with_sample_documents(self):
        """Test processing actual sample documents (PDF, DOCX, HTML)."""
        with tempfile.TemporaryDirectory() as vault_path:
            # Run the pipeline on the sample docs directory
            source = list_into_vector_db(
                source_pattern=str(SAMPLE_DOCS_DIR),
                vault_path=vault_path,
                overwrite=True,
            )
            results = list(source())

            # Verify we got results
            assert len(results) > 0

            # Verify sample documents were processed
            sources = [r.get("source", "") for r in results]
            assert any("SampleDocument.pdf" in p for p in sources)
            assert any("SampleDocument.docx" in p for p in sources)
            assert any("SampleDocument.html" in p for p in sources)
