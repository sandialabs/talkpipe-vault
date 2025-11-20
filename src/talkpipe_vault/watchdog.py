from queue import Queue
from talkpipe import source, register_source
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler


@register_source("fileWatcher")
@source()
def file_watcher(
    path: str,
    patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
    ignore_directories: bool = True,
    case_sensitive: bool = False,
    max_events: int | None = None,
):
    """
    Source that watches a directory for file changes and emits the paths of changed files.

    This source uses the watchdog library to monitor a specified directory for any file
    creation, modification, or deletion events. Events are queued and yielded from a
    blocking queue for controlled processing.

    Args:
        path: Directory path to watch for file changes
        patterns: List of glob patterns to match (e.g., ["*.txt", "*.md"]).
                 If None, matches all files.
        ignore_patterns: List of glob patterns to ignore (e.g., ["*.tmp", "*.log"]).
                        If None, no patterns are ignored.
        ignore_directories: Whether to ignore directory events. Default: True
        case_sensitive: Whether pattern matching is case-sensitive. Default: False
        max_events: Optional maximum number of events to process before stopping.
                   If None, runs indefinitely until interrupted.
    """
    event_queue = Queue()

    class WatchdogHandler(PatternMatchingEventHandler):
        def __init__(self, queue):
            super().__init__(
                patterns=patterns,
                ignore_patterns=ignore_patterns,
                ignore_directories=ignore_directories,
                case_sensitive=case_sensitive,
            )
            self.queue = queue

        def on_created(self, event):
            self.queue.put(event.src_path)

        def on_modified(self, event):
            self.queue.put(event.src_path)

        def on_deleted(self, event):
            self.queue.put(event.src_path)

    observer = Observer()
    handler = WatchdogHandler(queue=event_queue)
    observer.schedule(handler, path, recursive=True)
    observer.start()

    try:
        event_count = 0
        while max_events is None or event_count < max_events:
            # Block indefinitely until next event arrives
            file_path = event_queue.get()
            yield file_path
            event_count += 1
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()