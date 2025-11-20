import time
from pathlib import Path
from threading import Thread
import tempfile

import pytest
from talkpipe import compile
import talkpipe_vault.watchdog  # Ensure the source is registered


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

