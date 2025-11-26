import os
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
    path: Annotated[str, "Path to watch"],
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

    This source uses the watchdog library to monitor a specified directory for any file
    creation, modification, or deletion events. Events are queued and yielded from a
    blocking queue for controlled processing.

    Yields:
        dict: Event dictionary with keys:
            - "event": Event type ("created", "modified", or "deleted")
            - "path": Absolute path to the affected file

    Args:
        path: Directory path to watch for file changes
        patterns: List of glob patterns to match (e.g., ["*.txt", "*.md"]).
                 If None, matches all files.
        ignore_patterns: List of glob patterns to ignore (e.g., ["*.tmp", "*.log"]).
                        If None, no patterns are ignored. If ignore_common is True,
                        these are appended to the common ignore patterns.
        ignore_directories: Whether to ignore directory events. Default: True
        case_sensitive: Whether pattern matching is case-sensitive. Default: False
        max_events: Optional maximum number of events to process before stopping.
                   If None, runs indefinitely until interrupted.
        polling: Use polling-based observer instead of native filesystem events.
                Useful for network filesystems or when native events are unreliable.
                **RECOMMENDED for network filesystems** (NFS, SMB, CIFS, etc.).
                Default: False
        ignore_common: Ignore common temporary and hidden files (e.g., .*, *~, #*#,
                      *.swp, *.tmp, __pycache__). Default: True

    Raises:
        RuntimeError: If watchdog initialization times out. This typically indicates
                     a network filesystem or slow storage. Try using polling=True.
    """
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
                patterns=patterns,
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
    observer.schedule(handler, path, recursive=True)
    observer.start()

    # Block until observer is fully ready by using a sentinel file
    # This ensures inotify watches are actually registered before proceeding
    ready_queue = Queue()

    class ReadyHandler(FileSystemEventHandler):
        def on_created(self, event):
            if event.src_path.endswith(".watchdog_ready"):
                ready_queue.put(True)

    ready_handler = ReadyHandler()
    ready_watch = observer.schedule(ready_handler, path, recursive=False)

    # Create sentinel file and wait for its event
    sentinel_path = os.path.join(path, ".watchdog_ready")
    try:
        with open(sentinel_path, "w") as f:
            f.write("ready")
        # Wait for the sentinel event with timeout
        try:
            ready_queue.get(timeout=5.0)
        except Empty:
            # Timeout indicates filesystem events are slow or unavailable
            raise RuntimeError(
                f"Watchdog initialization timed out after 5 seconds for path: {path}\n"
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