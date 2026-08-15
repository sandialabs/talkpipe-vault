import os
import time
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Queue
from typing import Annotated, Any

from talkpipe import register_source, source
from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
    PatternMatchingEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.utils.patterns import match_any_paths

# Common patterns for temp files, hidden files, editor backups, etc.
COMMON_IGNORE_PATTERNS = [
    ".*",  # Hidden files/directories (start with .)
    "*~",  # Backup files (end with ~)
    "#*#",  # Emacs auto-save files
    ".#*",  # Emacs lock files
    "*.swp",  # Vim swap files
    "*.swo",  # Vim swap files
    "*.tmp",  # Temp files
    "*.temp",  # Temp files
    "~$*",  # Microsoft Office temp files
    "*.bak",  # Backup files
    "*.pyc",  # Python compiled files
    "__pycache__",  # Python cache directory
]


@register_source("fileWatcher")
@source()
def file_watcher(
    path: Annotated[
        str, "Path to watch (can include glob pattern like '/path/to/dir/*.txt')"
    ],
    patterns: Annotated[list[str] | None, "List of glob patterns to match"] = None,
    ignore_patterns: Annotated[
        list[str] | None, "List of glob patterns to ignore"
    ] = None,
    ignore_directories: Annotated[bool, "Whether to ignore directory events"] = True,
    case_sensitive: Annotated[
        bool, "Whether pattern matching is case-sensitive"
    ] = False,
    max_events: Annotated[int | None, "Maximum number of events to process"] = None,
    polling: Annotated[bool, "Use polling-based observer"] = False,
    ignore_common: Annotated[bool, "Ignore common temp/hidden files"] = True,
) -> Iterator[dict[str, Any]]:
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
    extracted_patterns: list[str] = []

    def has_glob(text: str) -> bool:
        return any(char in text for char in "*?[]")

    if has_glob(path):
        path_obj = Path(path)
        # Find the first parent that doesn't contain glob characters
        for parent in [path_obj, *list(path_obj.parents)]:
            parent_str = str(parent)
            if not has_glob(parent_str):
                watch_path = parent_str
                # Extract the pattern portion
                pattern = path[len(parent_str) :].lstrip(os.sep)
                if pattern:
                    extracted_patterns.append(pattern)
                break

    # Fail fast with a clear message; otherwise watchdog surfaces a bare
    # "FileNotFoundError: [Errno 2] No such file or directory" from inotify
    # that never names the offending path.
    expanded_watch_path = os.path.expanduser(watch_path)
    if not os.path.isdir(expanded_watch_path):
        raise FileNotFoundError(
            f"file_watcher: watch path '{watch_path}' does not exist or is not "
            "a directory. Create it first, or pass an existing directory "
            "(optionally with a glob pattern such as '/path/to/dir/*.txt')."
        )
    watch_path = expanded_watch_path

    # Combine extracted patterns with provided patterns
    final_patterns: list[str] | None
    if extracted_patterns:
        if patterns:
            final_patterns = extracted_patterns + list(patterns)
        else:
            final_patterns = extracted_patterns
    else:
        final_patterns = patterns

    # Build the final ignore patterns list
    final_ignore_patterns: list[str] | None
    if ignore_common:
        final_ignore_patterns = list(COMMON_IGNORE_PATTERNS)
        if ignore_patterns:
            final_ignore_patterns.extend(ignore_patterns)
    else:
        final_ignore_patterns = ignore_patterns

    event_queue: Queue[dict[str, Any]] = Queue()

    class WatchdogHandler(PatternMatchingEventHandler):
        def __init__(self, queue: Queue[dict[str, Any]]) -> None:
            super().__init__(
                patterns=final_patterns,
                ignore_patterns=final_ignore_patterns,
                ignore_directories=ignore_directories,
                case_sensitive=case_sensitive,
            )
            self.queue = queue

        def on_created(self, event: FileSystemEvent) -> None:
            self.queue.put({"event": "created", "path": event.src_path})

        def on_modified(self, event: FileSystemEvent) -> None:
            self.queue.put({"event": "modified", "path": event.src_path})

        def on_deleted(self, event: FileSystemEvent) -> None:
            self.queue.put({"event": "deleted", "path": event.src_path})

        # on_moved: handled by MoveOnlyHandler (bypasses pattern filter on src_path)

    # Move-only handler: receives ALL move events without pattern filtering.
    # PatternMatchingEventHandler can filter out moves when src_path is outside
    # the watch tree (e.g., mv from /tmp, drag-and-drop from file manager).
    class MoveOnlyHandler(FileSystemEventHandler):
        def __init__(
            self,
            queue: Queue[dict[str, Any]],
            watch_path: str,
            ignore_dirs: bool,
            patterns: list[str] | None,
            ignore_patterns: list[str] | None,
            case_sensitive: bool,
        ) -> None:
            self.queue = queue
            self.watch_path = os.path.normpath(watch_path)
            self.ignore_dirs = ignore_dirs
            self.patterns = patterns
            self.ignore_patterns = ignore_patterns
            self.case_sensitive = case_sensitive

        def on_moved(self, event: FileSystemEvent) -> None:
            if not hasattr(event, "dest_path") or not event.dest_path:
                return
            if self.ignore_dirs and getattr(event, "is_directory", False):
                return
            dest_path = os.fsdecode(event.dest_path)
            dest = os.path.normpath(dest_path)
            if not (
                dest.startswith(self.watch_path + os.sep) or dest == self.watch_path
            ):
                return
            if not match_any_paths(
                [dest_path],
                included_patterns=self.patterns,
                excluded_patterns=self.ignore_patterns,
                case_sensitive=self.case_sensitive,
            ):
                return
            self.queue.put({"event": "created", "path": dest_path})

    observer = PollingObserver() if polling else Observer()
    handler = WatchdogHandler(queue=event_queue)
    move_handler = MoveOnlyHandler(
        event_queue,
        watch_path,
        ignore_directories,
        final_patterns,
        final_ignore_patterns,
        case_sensitive,
    )
    observer.schedule(handler, watch_path, recursive=True)
    observer.schedule(move_handler, watch_path, recursive=True)
    observer.start()

    # Block until observer is fully ready (native Observer only).
    # PollingObserver does not need this: it polls the filesystem directly and
    # the sentinel check can fail in containers (bind mounts, permission issues).
    if not polling:
        ready_queue: Queue[bool] = Queue()

        class ReadyHandler(FileSystemEventHandler):
            def on_created(self, event: FileSystemEvent) -> None:
                if os.fsdecode(event.src_path).endswith(".watchdog_ready"):
                    ready_queue.put(True)

        ready_handler = ReadyHandler()
        ready_watch = observer.schedule(ready_handler, watch_path, recursive=False)

        sentinel_path = os.path.join(watch_path, ".watchdog_ready")
        try:
            with open(sentinel_path, "w") as f:
                f.write("ready")
            try:
                ready_queue.get(timeout=5.0)
            except Empty:
                raise RuntimeError(
                    f"Watchdog initialization timed out after 5 seconds for path: {watch_path}\n"
                    "This typically occurs with:\n"
                    "  - Network filesystems (NFS, SMB, CIFS)\n"
                    "  - Slow storage devices\n"
                    "  - Filesystems that don't support native event notifications\n"
                    "\n"
                    "Solution: Add the --polling flag to use polling-based file monitoring.\n"
                    "Example: vault-watch-into-vectordb <path> --polling ..."
                ) from None
        finally:
            if os.path.exists(sentinel_path):
                os.remove(sentinel_path)
            observer.unschedule(ready_watch)

        # Drain any sentinel-related events from the main queue
        while True:
            try:
                evt = event_queue.get_nowait()
                if ".watchdog_ready" not in evt.get("path", ""):
                    event_queue.put(evt)
                    break
            except Empty:
                break
    else:
        # Give PollingObserver time for first snapshot
        time.sleep(0.5)

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
