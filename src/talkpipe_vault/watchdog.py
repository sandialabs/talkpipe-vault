import os
from pathlib import Path
from typing import Annotated
from queue import Queue, Empty
from talkpipe import source, register_source
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import PatternMatchingEventHandler, FileSystemEventHandler


# Common patterns for temp files, hidden files, editor backups, etc.
COMMON_IGNORE_PATTERNS = [
    ".*",        # Hidden files/directories (start with .)
    "*~",        # Backup files (end with ~)
    "#*#",       # Emacs auto-save files
    ".#*",       # Emacs lock files
    "*.swp",     # Vim swap files
    "*.swo",     # Vim swap files
    "*.tmp",     # Temp files
    "*.temp",    # Temp files
    "~$*",       # Microsoft Office temp files
    "*.bak",     # Backup files
    "*.pyc",     # Python compiled files
    "__pycache__",  # Python cache directory
]


@register_source("fileWatcher")
@source()
def file_watcher(
    path: Annotated[str, "Path to watch (can include glob pattern like '/path/to/dir/*.txt')"],
    patterns: Annotated[list[str] | None, "List of glob patterns to match"] = None,
    ignore_patterns: Annotated[list[str] | None, "List of glob patterns to ignore"] = None,
    ignore_directories: Annotated[bool, "Whether to ignore directory events"] = True,
    case_sensitive: Annotated[bool, "Whether pattern matching is case-sensitive"] = False,
    max_events: Annotated[int | None, "Maximum number of events to process"] = None,
    polling: Annotated[bool, "Use polling-based observer"] = False,
    ignore_common: Annotated[bool, "Ignore common temp/hidden files"] = True,
):
    """
    Source that watches a directory for file changes and emits file events.

    Uses the watchdog library to monitor a directory for file creation, modification,
    or deletion events. Events are queued and yielded for controlled processing.
    Supports both native filesystem events and polling mode for network filesystems.

    The path parameter can be either a directory path or include a glob pattern
    (e.g., '/path/to/dir/*.txt'). If a glob pattern is detected, it will be extracted
    and added to the patterns list, and the directory portion will be watched.

    Yields dicts with the following structure:
        - "event": str - Event type ("created", "modified", or "deleted")
        - "path": str - Absolute path to the affected file

    Raises RuntimeError if watchdog initialization times out (typically on network
    filesystems - use polling=True in that case).
    """
    # Parse glob pattern from path if present
    watch_path = path
    extracted_patterns = []

    # Check if path contains glob characters
    if any(char in path for char in ['*', '?', '[', ']']):
        path_obj = Path(path)
        # Find the first parent that doesn't contain glob characters
        for parent in [path_obj] + list(path_obj.parents):
            parent_str = str(parent)
            if not any(char in parent_str for char in ['*', '?', '[', ']']):
                watch_path = parent_str
                # Extract the pattern portion
                pattern = path[len(parent_str):].lstrip(os.sep)
                if pattern:
                    extracted_patterns.append(pattern)
                break

    # Combine extracted patterns with provided patterns
    if extracted_patterns:
        if patterns:
            final_patterns = extracted_patterns + list(patterns)
        else:
            final_patterns = extracted_patterns
    else:
        final_patterns = patterns

    # Build the final ignore patterns list
    if ignore_common:
        final_ignore_patterns = list(COMMON_IGNORE_PATTERNS)
        if ignore_patterns:
            final_ignore_patterns.extend(ignore_patterns)
    else:
        final_ignore_patterns = ignore_patterns

    event_queue = Queue()

    class WatchdogHandler(PatternMatchingEventHandler):
        def __init__(self, queue):
            super().__init__(
                patterns=final_patterns,
                ignore_patterns=final_ignore_patterns,
                ignore_directories=ignore_directories,
                case_sensitive=case_sensitive,
            )
            self.queue = queue

        def on_created(self, event):
            self.queue.put({"event": "created", "path": event.src_path})

        def on_modified(self, event):
            self.queue.put({"event": "modified", "path": event.src_path})

        def on_deleted(self, event):
            self.queue.put({"event": "deleted", "path": event.src_path})

    observer = PollingObserver() if polling else Observer()
    handler = WatchdogHandler(queue=event_queue)
    observer.schedule(handler, watch_path, recursive=True)
    observer.start()

    # Block until observer is fully ready by using a sentinel file
    # This ensures inotify watches are actually registered before proceeding
    ready_queue = Queue()

    class ReadyHandler(FileSystemEventHandler):
        def on_created(self, event):
            if event.src_path.endswith(".watchdog_ready"):
                ready_queue.put(True)

    ready_handler = ReadyHandler()
    ready_watch = observer.schedule(ready_handler, watch_path, recursive=False)

    # Create sentinel file and wait for its event
    sentinel_path = os.path.join(watch_path, ".watchdog_ready")
    try:
        with open(sentinel_path, "w") as f:
            f.write("ready")
        # Wait for the sentinel event with timeout
        try:
            ready_queue.get(timeout=5.0)
        except Empty:
            # Timeout indicates filesystem events are slow or unavailable
            raise RuntimeError(
                f"Watchdog initialization timed out after 5 seconds for path: {watch_path}\n"
                "This typically occurs with:\n"
                "  - Network filesystems (NFS, SMB, CIFS)\n"
                "  - Slow storage devices\n"
                "  - Filesystems that don't support native event notifications\n"
                "\n"
                "Solution: Add the --polling flag to use polling-based file monitoring.\n"
                "Example: vault-watch-into-vectordb <path> --polling ..."
            )
    finally:
        # Clean up sentinel file
        if os.path.exists(sentinel_path):
            os.remove(sentinel_path)
        observer.unschedule(ready_watch)

    # Drain any sentinel-related events from the main queue
    while True:
        try:
            evt = event_queue.get_nowait()
            if ".watchdog_ready" not in evt.get("path", ""):
                # Put back non-sentinel events
                event_queue.put(evt)
                break
        except Empty:
            break

    try:
        event_count = 0
        while max_events is None or event_count < max_events:
            # Block indefinitely until next event arrives
            file_event = event_queue.get()
            # Filter out any sentinel events that may have slipped through
            if ".watchdog_ready" not in file_event.get("path", ""):
                yield file_event
                event_count += 1
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()