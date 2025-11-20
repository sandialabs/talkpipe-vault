from queue import Queue
from talkpipe import source, register_source
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


@register_source("fileWatcher")
@source()
def file_watcher(path: str, max_events: int | None = None):
    """
    Source that watches a directory for file changes and emits the paths of changed files.

    This source uses the watchdog library to monitor a specified directory for any file
    creation, modification, or deletion events. Events are queued and yielded from a
    blocking queue for controlled processing.

    Args:
        path: Directory path to watch for file changes
        max_events: Optional maximum number of events to process before stopping.
                   If None, runs indefinitely until interrupted.
    """
    event_queue = Queue()

    class WatchdogHandler(FileSystemEventHandler):
        def __init__(self, queue):
            self.queue = queue

        def on_created(self, event):
            if not event.is_directory:
                self.queue.put(event.src_path)

        def on_modified(self, event):
            if not event.is_directory:
                self.queue.put(event.src_path)

        def on_deleted(self, event):
            if not event.is_directory:
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