import tempfile
import time
from pathlib import Path
from threading import Thread

from talkpipe import compile

from talkpipe_vault.watchdog import file_watcher


class TestFileWatcher:
    """Tests for the file_watcher TalkPipe source."""

    def test_file_creation(self):
        """Test that file creation events are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                script = (
                    f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                )
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
            event = results[0][0]
            assert event["path"] == str(test_file)
            assert event["event"] == "created"

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
                script = (
                    f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                )
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
            event = results[0][0]
            assert event["path"] == str(test_file)
            assert event["event"] == "modified"

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
                script = (
                    f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                )
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
            event = results[0][0]
            assert event["path"] == str(test_file)
            assert event["event"] == "deleted"

    def test_file_moved_into_watch_dir(self):
        """Test that files moved into the watch directory are detected (e.g., drag-and-drop)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create file outside watch dir, then move it in
            outside = Path(tmpdir).parent / "moved_file.txt"
            outside.write_text("Moved content")
            dest = Path(tmpdir) / "moved_file.txt"

            results = []

            def run_pipeline():
                script = (
                    f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                )
                compiled = compile(script)
                ans = list(compiled())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            time.sleep(0.5)

            # Move file into watch directory (simulates drag-and-drop)
            outside.rename(dest)

            thread.join(timeout=5.0)

            assert len(results) == 1
            event = results[0][0]
            assert event["path"] == str(dest)
            assert event["event"] == "created"

    def test_multiple_events(self):
        """Test that multiple file events are detected in order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Each file creation triggers both 'created' and 'modified' events
                # So 3 files = 6 events total
                script = (
                    f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=6] | toList'
                )
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
            paths = [event["path"] for event in results[0]]
            assert sum(file1_str == path for path in paths) == 2
            assert sum(file2_str == path for path in paths) == 2
            assert sum(file3_str == path for path in paths) == 2

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
                script = (
                    f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                )
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
            event = results[0][0]
            assert event["path"] == str(test_file)

    def test_directory_events_ignored(self):
        """Test that directory creation/modification events are not yielded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                script = (
                    f'INPUT FROM fileWatcher[path="{tmpdir}", max_events=1] | toList'
                )
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
            event = results[0][0]
            assert event["path"] == str(test_file)
            assert str(new_dir) not in event["path"]

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
            unique_paths = {event["path"] for event in results}
            assert str(txt_file) in unique_paths
            assert str(txt_file2) in unique_paths
            assert str(md_file) not in unique_paths
            # Verify .md file never appeared in any result
            assert not any(str(md_file) == event["path"] for event in results)

    def test_multiple_patterns(self):
        """Test that multiple patterns can be specified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Watch both .txt and .md files (2 files x 2 events each = 4 events)
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
            unique_paths = {event["path"] for event in results}
            assert str(txt_file) in unique_paths
            assert str(md_file) in unique_paths
            assert str(py_file) not in unique_paths

    def test_custom_ignore_patterns(self):
        """Test that custom ignore patterns can be specified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Ignore .tmp and .bak files (only data.txt generates events = 2 events)
                script = file_watcher(
                    tmpdir, ignore_patterns=["*.tmp", "*.bak"], max_events=2
                )
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
            unique_paths = {event["path"] for event in results}
            assert str(normal_file) in unique_paths
            assert str(tmp_file) not in unique_paths
            assert str(bak_file) not in unique_paths

    def test_case_sensitive_matching(self):
        """Test case-sensitive pattern matching."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Case-sensitive match for .txt only (not .TXT) - only lowercase .txt matches = 2 events
                script = file_watcher(
                    tmpdir, patterns=["*.txt"], case_sensitive=True, max_events=2
                )
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
            unique_paths = {event["path"] for event in results}
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
            unique_paths = {event["path"] for event in results}
            assert str(upper_file) in unique_paths
            assert str(lower_file) in unique_paths

    def test_chatterlang_array_ignore_patterns(self):
        """Test that array parameters can be specified in chatterlang scripts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Use chatterlang syntax with array parameter for ignore_patterns
                script = f'INPUT FROM fileWatcher[path="{tmpdir}", ignore_patterns=["*.tmp", "*.bak"], max_events=2] | toList'
                compiled = compile(script)
                ans = list(compiled())
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
            assert len(results) == 1
            assert isinstance(results[0], list)
            assert len(results[0]) == 2  # created + modified events
            paths = [event["path"] for event in results[0]]
            assert all(str(normal_file) == path for path in paths)
            assert not any(str(tmp_file) == path for path in paths)
            assert not any(str(bak_file) == path for path in paths)

    def test_ignore_common_false(self):
        """Test that ignore_common=False allows common temp files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Disable common ignore patterns to capture temp files
                script = file_watcher(tmpdir, ignore_common=False, max_events=4)
                ans = list(script())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create common temp files that would normally be ignored
            swp_file = Path(tmpdir) / "test.swp"
            swp_file.write_text("Vim swap file")
            time.sleep(0.2)

            tmp_file = Path(tmpdir) / "test.tmp"
            tmp_file.write_text("Temp file")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify both files were captured (common patterns not ignored)
            unique_paths = {event["path"] for event in results}
            assert str(swp_file) in unique_paths
            assert str(tmp_file) in unique_paths

    def test_ignore_common_false_with_custom_patterns(self):
        """Test that ignore_common=False uses only custom ignore patterns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Disable common ignore patterns but add custom ones
                script = file_watcher(
                    tmpdir, ignore_common=False, ignore_patterns=["*.log"], max_events=2
                )
                ans = list(script())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create .tmp file first (should NOT be ignored since common patterns disabled)
            tmp_file = Path(tmpdir) / "test.tmp"
            tmp_file.write_text("Temp file")
            time.sleep(0.3)

            # Create .log file (should be ignored by custom pattern)
            log_file = Path(tmpdir) / "test.log"
            log_file.write_text("Log file")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify .tmp was captured but .log was ignored
            unique_paths = {event["path"] for event in results}
            assert str(tmp_file) in unique_paths
            assert str(log_file) not in unique_paths

    def test_polling_observer(self):
        """Test that polling observer mode works correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Use polling observer instead of native observer
                script = file_watcher(tmpdir, polling=True, max_events=1)
                ans = list(script())
                results.extend(ans)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Polling observer needs more time to initialize
            time.sleep(1.5)

            # Create a file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Content for polling test")

            # Wait for thread to complete (polling is slower)
            thread.join(timeout=5.0)

            # Verify at least one event was captured with polling observer
            assert len(results) >= 1
            assert str(test_file) in results[0]["path"]

    def test_keyboard_interrupt_handling(self):
        """Test that KeyboardInterrupt is handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []
            exception_caught = []

            def run_pipeline():
                try:
                    # Start file watcher with no max_events (runs indefinitely)
                    script = file_watcher(tmpdir)
                    # Process first event, then raise KeyboardInterrupt
                    for count, event in enumerate(script(), start=1):
                        results.append(event)
                        if count >= 1:
                            # Manually raise KeyboardInterrupt after first event
                            raise KeyboardInterrupt()
                except KeyboardInterrupt:
                    exception_caught.append(True)

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start
            time.sleep(0.5)

            # Create a file to trigger an event
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Test content")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify KeyboardInterrupt was raised and caught
            assert len(exception_caught) > 0
            assert len(results) >= 1

    def test_sentinel_event_draining(self):
        """Test that sentinel events are properly drained from the queue."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = []

            def run_pipeline():
                # Create a file before starting the watcher to potentially trigger
                # sentinel-related queue issues
                script = file_watcher(tmpdir, max_events=1)
                ans = list(script())
                results.extend(ans)

            # Pre-create a file to add complexity to the queue
            pre_file = Path(tmpdir) / "pre_existing.txt"
            pre_file.write_text("Pre-existing")

            thread = Thread(target=run_pipeline, daemon=True)
            thread.start()

            # Give watcher time to start and process sentinel
            time.sleep(0.5)

            # Create a new file after watcher is ready
            test_file = Path(tmpdir) / "new_file.txt"
            test_file.write_text("New content")

            # Wait for thread to complete
            thread.join(timeout=3.0)

            # Verify that we got exactly 1 event and it's not the sentinel
            assert len(results) == 1
            assert ".watchdog_ready" not in results[0]["path"]
            # Should be the new file we created
            assert "new_file.txt" in results[0]["path"]

    def test_missing_watch_path_raises_clear_error(self):
        """A nonexistent watch path must name itself in the error.

        Without validation, watchdog surfaces a bare inotify FileNotFoundError
        that never says which path was the problem.
        """
        import pytest

        pipeline = file_watcher(path="/path/that/does/not/exist")
        with pytest.raises(FileNotFoundError, match="/path/that/does/not/exist"):
            list(pipeline())

    def test_tilde_watch_path_is_expanded(self, tmp_path, monkeypatch):
        """A ~/... watch path should refer to the home directory, not ./~."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "watched").mkdir()

        results = []

        def run_pipeline():
            pipeline = file_watcher(path="~/watched", max_events=1)
            results.extend(list(pipeline()))

        thread = Thread(target=run_pipeline, daemon=True)
        thread.start()
        time.sleep(0.5)
        (tmp_path / "watched" / "hello.txt").write_text("hi")
        thread.join(timeout=3.0)

        assert len(results) == 1
        assert results[0]["event"] == "created"
