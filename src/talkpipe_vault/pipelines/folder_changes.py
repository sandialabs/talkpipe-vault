"""folderChanges: a folder's document files as a stream of changes.

The source emits every document file under a folder once, then each file
again as it is added, changed or deleted. It uses folder mode's file rules
(hidden paths and temporary files skipped, only files a talkpipe extractor
reads, symlinks only inside the folder) and its change detection (a
watcher plus periodic rescans, and a content hash so a touched file is not
reported as changed). It reads no contents and indexes nothing: a pipeline
reads the files itself, e.g. with talkpipe's readFile.

What has been emitted is remembered in a small sqlite file per consumer,
``<folder>/.tpstream/<name>.db``, so a restarted pipeline emits only what
changed while it was stopped. It is kept apart from ``.tpvault`` so that
rebuilding or deleting a folder's index never makes a stream emit the whole
folder again. Folders that cannot hold it (read-only, network shares,
synced folders, a set ``TALKPIPE_VAULT_ROOT``) keep it under
``<vault root or home>/folder-streams/`` instead, like an index.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import filelock
from talkpipe import register_source, source

from .folder_files import (
    ChangeTracker,
    covering_subtrees,
    diff_files,
    prepare_index_dir,
    resolve_index_location,
    supported_extensions,
)
from .folder_index import default_index_home

logger = logging.getLogger(__name__)

STREAM_STATE_DIRNAME = ".tpstream"
EXTERNAL_STREAM_DIRNAME = "folder-streams"
DEFAULT_STREAM_NAME = "default"
_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# How often the stream checks for reported changes and due rescans.
POLL_SECONDS = 0.5


def stream_state_path(
    root: str | Path, name: str = DEFAULT_STREAM_NAME, home: Path | None = None
) -> Path:
    """Where the stream *name* over the folder *root* keeps its state.

    In ``<root>/.tpstream/`` when the folder can hold it, else in app data
    (*home*, default the vault root or home). *name* must be a plain file
    name, so a pipeline cannot place the state outside that directory.
    """
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"stream name {name!r} must be letters, digits, '.', '_' or '-'"
        )
    location = resolve_index_location(
        root,
        home if home is not None else default_index_home(),
        dirname=STREAM_STATE_DIRNAME,
        external_dirname=EXTERNAL_STREAM_DIRNAME,
    )
    if location.read_only:
        raise RuntimeError(
            f"{location.path} is read-only; pass state= to keep this stream's "
            "state somewhere writable"
        )
    # The state is small and belongs with the documents in a restore, so it
    # carries no CACHEDIR.TAG (backup tools would skip it).
    prepare_index_dir(location, Path(os.path.realpath(root)), cache_tag=False)
    return location.path / f"{name}.db"


class FolderChanges:
    """One folder's document files, reported as they appear, change and go.

    ``changes()`` compares what is on disk with what was emitted and yields
    an item per difference. A file's new state is recorded only when the
    consumer asks for the next item, so a file the pipeline was still
    handling when it stopped is emitted again on restart (at least once,
    never silently lost).
    """

    def __init__(self, root: str | Path, state: str | Path):
        self.root = Path(os.path.realpath(Path(root).expanduser()))
        if not self.root.is_dir():
            raise ValueError(f"{root} is not a folder")
        self.state_path = str(state)
        self.tracker = ChangeTracker(self.root)
        self._lock = filelock.FileLock(self.state_path + ".lock", is_singleton=False)
        self._conn: sqlite3.Connection | None = None

    @property
    def watch_mode(self) -> str:
        return self.tracker.watch_mode

    def open(self, watch: bool = True) -> None:
        try:
            self._lock.acquire(timeout=0)
        except filelock.Timeout:
            raise RuntimeError(
                f"Another pipeline is already reading {self.root} with the state "
                f"{self.state_path}; give this one a different name"
            ) from None
        try:
            # A pipeline may resume this generator from another thread (fork
            # does); it is never resumed from two at once.
            self._conn = sqlite3.connect(self.state_path, check_same_thread=False)
            with self._conn:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS files (relpath TEXT PRIMARY KEY, "
                    "size INTEGER, mtime_ns INTEGER, sha256 TEXT)"
                )
        except Exception:
            self._lock.release()
            raise
        if watch:
            self.tracker.start_watching()

    def close(self) -> None:
        self.tracker.stop_watching()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._lock.release()

    def note_change(self, path: str) -> None:
        """Record that *path* changed; it is compared after a short delay."""
        self.tracker.note_change(path)

    def due(self, now: float) -> list[str] | None:
        """The subtrees to compare now ([""] is the whole folder), or None."""
        return self.tracker.due(now)

    def changes(self, subtrees: list[str]) -> Iterator[dict[str, Any]]:
        """Yield an item for every difference at or below *subtrees*."""
        extensions = supported_extensions()
        for subtree in covering_subtrees(subtrees):
            yield from self._compare(subtree, extensions)

    def _compare(self, subtree: str, extensions: set[str]) -> Iterator[dict[str, Any]]:
        assert self._conn is not None  # nosec B101 - set by open()
        known = self._rows_under(subtree)
        for change in diff_files(self.root, subtree, known, extensions, hash_new=True):
            rel = change.relpath
            if change.kind == "deleted" or change.stat is None:
                yield self._item("deleted", rel)
                with self._conn:
                    self._conn.execute("DELETE FROM files WHERE relpath = ?", (rel,))
                continue
            if change.kind != "touched":
                yield self._item(change.kind, rel)
            # A touched file only has its recorded stat brought up to date.
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                    (rel, change.stat.st_size, change.stat.st_mtime_ns, change.digest),
                )

    def _rows_under(self, subtree: str) -> dict[str, tuple[int, int, str | None]]:
        assert self._conn is not None  # nosec B101 - set by open()
        query = "SELECT relpath, size, mtime_ns, sha256 FROM files"
        params: tuple[Any, ...] = ()
        if subtree:
            # substr rather than LIKE: relpaths may contain % and _.
            query += " WHERE relpath = ? OR substr(relpath, 1, ?) = ?"
            params = (subtree, len(subtree) + 1, subtree + "/")
        rows = self._conn.execute(query, params).fetchall()
        return {row[0]: (row[1], row[2], row[3]) for row in rows}

    def _item(self, event: str, rel: str) -> dict[str, Any]:
        return {
            "event": event,
            "path": str(self.root / rel),
            "relpath": rel,
            "folder": str(self.root),
        }


@register_source("folderChanges")
@source()
def folder_changes(
    path: Annotated[str, "Folder to report on, with its whole tree"],
    name: Annotated[
        str, "Name of this pipeline's record of what it has been sent"
    ] = DEFAULT_STREAM_NAME,
    state: Annotated[
        str | None, "Path of the state file, in place of the name's default"
    ] = None,
    watch: Annotated[
        bool, "Keep reporting changes; False stops after the first pass"
    ] = True,
    max_events: Annotated[int | None, "Stop after this many items"] = None,
) -> Iterator[dict[str, Any]]:
    """Emit a folder's document files, then each change to them.

    Emits one dict per file:
        - "event": "added", "changed" or "deleted"
        - "path": the file's absolute path
        - "relpath": its path relative to the folder ("/"-separated)
        - "folder": the folder's absolute path

    The first pass emits every file not emitted before (all of them on a
    first run) and every recorded file that is gone; after that a file is
    emitted again when its content changes, and as "deleted" when it goes.
    A rename is a "deleted" then an "added". Hidden files and folders,
    temporary files and files no talkpipe extractor reads are skipped.

    A file counts as handled when the pipeline asks for the next item, so
    one being handled when the pipeline stopped is emitted again on
    restart; segments that buffer items (flushN, fork, ...) lose that
    guarantee for what they hold. Pipelines reading the same folder
    independently each need their own name; one name is one record.
    """
    root = Path(path).expanduser()
    state_file = Path(state).expanduser() if state else stream_state_path(root, name)
    reader = FolderChanges(root, state_file)
    reader.open(watch=watch)
    emitted = 0
    try:
        while max_events is None or emitted < max_events:
            subtrees = reader.due(time.monotonic())
            if subtrees is not None:
                for item in reader.changes(subtrees):
                    # Checked before yielding rather than after, so the last
                    # item is recorded (that happens when the next is asked
                    # for) instead of being emitted again on the next run.
                    if max_events is not None and emitted >= max_events:
                        return
                    yield item
                    emitted += 1
            if not watch:
                return
            try:
                time.sleep(POLL_SECONDS)
            except KeyboardInterrupt:
                # Ctrl+C is the documented way to stop: end the stream.
                return
    finally:
        reader.close()
