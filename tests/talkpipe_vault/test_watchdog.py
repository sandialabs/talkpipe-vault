import time
from pathlib import Path
from threading import Thread
import tempfile

import pytest
from talkpipe import compile
from talkpipe_vault.watchdog import file_watcher


class TestFileWatcher:
    """Tests for the file_watcher TalkPipe source."""

    def test_file_creation(self):
        """Test that file creation events are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                script = f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                compiled = compile(script)
                ans = list(compiled())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create a file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello, World!")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify event was captured
            assert len(results) == 1
            assert isinstance(results[0], list)
            assert len(results[0]) == 1
            assert str(test_file) in results[0][0]

    def test_file_modification(self):
        """Test that file modification events are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create initial file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Initial content")

            # Give filesystem time to settle
            time.sleep(0.5)

            results = []

            def run_pipeline():
                script = f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                compiled = compile(script)
                ans = list(compiled())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Modify the file
            test_file.write_text("Modified content")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify event was captured
            assert len(results) == 1
            assert str(test_file) in results[0][0]

    def test_file_deletion(self):
        """Test that file deletion events are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create initial file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Content to delete")

            # Give filesystem time to settle
            time.sleep(0.5)

            results = []

            def run_pipeline():
                script = f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                compiled = compile(script)
                ans = list(compiled())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Delete the file
            test_file.unlink()

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify event was captured
            assert len(results) == 1
            assert str(test_file) in results[0][0]

    def test_multiple_events(self):
        """Test that multiple file events are detected in order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Each file creation triggers both 'created' and 'modified' events
                # So 3 files = 6 events total
                script = f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=6] | toList'
                compiled = compile(script)
                ans = list(compiled())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create multiple files
            file1 = Path(tmpdir) / "file1.txt"
            file2 = Path(tmpdir) / "file2.txt"
            file3 = Path(tmpdir) / "file3.txt"

            file1.write_text("Content 1")
            time.sleep(0.2)
            file2.write_text("Content 2")
            time.sleep(0.2)
            file3.write_text("Content 3")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify thread stopped
            assert not thread.is_alive()

            # Verify all events were captured (6 events: 2 per file)
            assert len(results) == 1
            assert len(results[0]) == 6
            # Each file should appear in the results (created + modified)
            file1_str = str(file1)
            file2_str = str(file2)
            file3_str = str(file3)
            assert sum(file1_str in path for path in results[0]) == 2
            assert sum(file2_str in path for path in results[0]) == 2
            assert sum(file3_str in path for path in results[0]) == 2

    def test_max_events_limit(self):
        """Test that max_events parameter limits the number of events processed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            max_events = 2
            results = []

            def run_pipeline():
                script = f'INPUT FROM fileWatcher[path="{tmpdir}", max_events={max_events}] | toList'
                compiled = compile(script)
                ans = list(compiled())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create more files than max_events
            for i in range(5):
                file_path = Path(tmpdir) / f"file{i}.txt"
                file_path.write_text(f"Content {i}")
                time.sleep(0.2)

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify thread stopped
            assert not thread.is_alive()

            # Verify only max_events were processed
            assert len(results) == 1
            assert len(results[0]) == max_events

    def test_recursive_watching(self):
        """Test that subdirectories are watched recursively."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create subdirectory
            subdir = Path(tmpdir) / "subdir"
            subdir.mkdir()

            results = []

            def run_pipeline():
                script = f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                compiled = compile(script)
                ans = list(compiled())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create file in subdirectory
            test_file = subdir / "test.txt"
            test_file.write_text("Content in subdirectory")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify thread stopped
            assert not thread.is_alive()

            # Verify event was captured
            assert len(results) == 1
            assert str(test_file) in results[0][0]

    def test_directory_events_ignored(self):
        """Test that directory creation/modification events are not yielded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                script = f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                compiled = compile(script)
                ans = list(compiled())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create a directory (should be ignored)
            new_dir = Path(tmpdir) / "new_directory"
            new_dir.mkdir()

            # Wait a bit to see if any events come through
            time.sleep(0.5)

            # Create a file to trigger an event and end the watcher
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Actual file")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify thread stopped
            assert not thread.is_alive()

            # Verify only the file event was captured, not the directory
            assert len(results) == 1
            assert len(results[0]) == 1
            assert str(test_file) in results[0][0]
            assert str(new_dir) not in results[0][0]

    def test_patterns_filter(self):
        """Test that patterns parameter filters files by extension."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Each file can trigger multiple events (created + modified)
                # So we need enough events to capture both files
                script = file_watcher(tmpdir, patterns=["*.txt"], max_events=4)
                ans = list(script())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create files with different extensions
            txt_file = Path(tmpdir) / "test.txt"
            md_file = Path(tmpdir) / "test.md"
            txt_file2 = Path(tmpdir) / "test2.txt"

            txt_file.write_text("Text file")
            time.sleep(0.2)
            md_file.write_text("Markdown file")  # Should be ignored
            time.sleep(0.2)
            txt_file2.write_text("Another text file")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify only .txt files were captured (get unique paths)
            unique_paths = set(results)
            assert str(txt_file) in unique_paths
            assert str(txt_file2) in unique_paths
            assert str(md_file) not in unique_paths
            # Verify .md file never appeared in any result
            assert not any(str(md_file) in path for path in results)

    def test_multiple_patterns(self):
        """Test that multiple patterns can be specified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Watch both .txt and .md files (2 files × 2 events each = 4 events)
                script = file_watcher(tmpdir, patterns=["*.txt", "*.md"], max_events=4)
                ans = list(script())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create files with different extensions
            txt_file = Path(tmpdir) / "test.txt"
            md_file = Path(tmpdir) / "test.md"
            py_file = Path(tmpdir) / "test.py"

            txt_file.write_text("Text file")
            time.sleep(0.2)
            md_file.write_text("Markdown file")
            time.sleep(0.2)
            py_file.write_text("Python file")  # Should be ignored

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify both .txt and .md files were captured, but not .py
            unique_paths = set(results)
            assert str(txt_file) in unique_paths
            assert str(md_file) in unique_paths
            assert str(py_file) not in unique_paths

    def test_custom_ignore_patterns(self):
        """Test that custom ignore patterns can be specified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Ignore .tmp and .bak files (only data.txt generates events = 2 events)
                script = file_watcher(tmpdir, ignore_patterns=["*.tmp", "*.bak"], max_events=2)
                ans = list(script())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create ignored files
            tmp_file = Path(tmpdir) / "temp.tmp"
            tmp_file.write_text("Temp file")
            time.sleep(0.2)

            bak_file = Path(tmpdir) / "backup.bak"
            bak_file.write_text("Backup file")
            time.sleep(0.2)

            # Create normal file
            normal_file = Path(tmpdir) / "data.txt"
            normal_file.write_text("Normal file")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify only normal file was captured
            unique_paths = set(results)
            assert str(normal_file) in unique_paths
            assert str(tmp_file) not in unique_paths
            assert str(bak_file) not in unique_paths

    def test_case_sensitive_matching(self):
        """Test case-sensitive pattern matching."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Case-sensitive match for .txt only (not .TXT) - only lowercase .txt matches = 2 events
                script = file_watcher(tmpdir, patterns=["*.txt"], case_sensitive=True, max_events=2)
                ans = list(script())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create uppercase extension file
            upper_file = Path(tmpdir) / "test.TXT"
            upper_file.write_text("Upper case")
            time.sleep(0.2)

            # Create lowercase extension file
            lower_file = Path(tmpdir) / "test.txt"
            lower_file.write_text("Lower case")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify only lowercase .txt was captured (case-sensitive)
            unique_paths = set(results)
            assert str(lower_file) in unique_paths
            assert str(upper_file) not in unique_paths

    def test_case_insensitive_matching(self):
        """Test case-insensitive pattern matching (default)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Case-insensitive match (default) - both .txt and .TXT should match
                script = file_watcher(tmpdir, patterns=["*.txt"], max_events=4)
                ans = list(script())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create files with different case extensions
            upper_file = Path(tmpdir) / "test.TXT"
            lower_file = Path(tmpdir) / "test.txt"

            upper_file.write_text("Upper case")
            time.sleep(0.2)
            lower_file.write_text("Lower case")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify both files were captured (case-insensitive)
            unique_paths = set(results)
            assert str(upper_file) in unique_paths
            assert str(lower_file) in unique_paths

