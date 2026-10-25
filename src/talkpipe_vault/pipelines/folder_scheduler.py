"""The background thread that indexes every open folder."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filelock

if TYPE_CHECKING:
    from .folder_index import FolderIndex

logger = logging.getLogger(__name__)

INGEST_LOCK_FILENAME = "ingest.lock"


class FolderScheduler:
    """One background thread doing the indexing for every open folder.

    Keyword work for all folders comes before any vector work, and changed
    files go through the keyword phase before any vector backlog. A
    machine-wide ``ingest.lock`` is held for each batch, so two vault
    processes interleave instead of both reaching ingestion's memory peak at
    once; ``ingesting()`` lets another indexing run in this process take the
    same turn.
    """

    def __init__(self, ingest_lock_dir: Path | None = None):
        self._folders: list[FolderIndex] = []
        self._folders_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        # Folders to release, and maintenance to run, on the worker thread
        # (never concurrently with a batch); each entry's event is set when
        # it is done.
        self._closing: list[tuple[FolderIndex, threading.Event]] = []
        self._tasks: list[tuple[FolderIndex, Callable[[], None], threading.Event]] = []
        self._ingest_lock = (
            filelock.FileLock(
                str(ingest_lock_dir / INGEST_LOCK_FILENAME), is_singleton=False
            )
            if ingest_lock_dir is not None
            else None
        )
        if ingest_lock_dir is not None:
            ingest_lock_dir.mkdir(parents=True, exist_ok=True)

    def folders(self) -> list[FolderIndex]:
        with self._folders_lock:
            return list(self._folders)

    def add(self, folder: FolderIndex) -> None:
        with self._folders_lock:
            self._folders.append(folder)
        self.wake()

    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def remove(self, folder: FolderIndex, timeout: float = 30.0) -> bool:
        """Stop indexing *folder* and release its writer role.

        Waits (up to *timeout*) for the batch in progress to finish, so the
        index can be deleted afterwards; returns False if it did not.
        """
        with self._folders_lock:
            if folder in self._folders:
                self._folders.remove(folder)
            if not self._running():
                folder.close()
                return True
            done = threading.Event()
            self._closing.append((folder, done))
        self.wake()
        return done.wait(timeout)

    def submit(self, folder: FolderIndex, task: Callable[[], None]) -> threading.Event:
        """Run *task* (maintenance on *folder*) on the worker, between batches.

        The returned event is set once it has run; its outcome is recorded
        in the folder's ``error``.
        """
        done = threading.Event()
        with self._folders_lock:
            self._tasks.append((folder, task, done))
        self.wake()
        return done

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="folder-indexer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Finish the current batch and release every folder."""
        self._stop.set()
        self.wake()
        if self._thread is not None:
            self._thread.join(timeout)
        with self._folders_lock:
            closing = [folder for folder, _ in self._closing]
            self._closing.clear()
        for folder in self.folders() + closing:
            with contextlib.suppress(Exception):
                folder.close()

    def wake(self) -> None:
        self._wake.set()

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """Hold off folder indexing (while a named-vault index job runs)."""
        self._paused.set()
        try:
            yield
        finally:
            self._paused.clear()
            self.wake()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.step()
            except Exception:
                logger.exception("Folder indexing step failed")
                worked = False
            if not worked:
                self._wake.wait(1.0)
                self._wake.clear()

    @contextlib.contextmanager
    def _ingesting(self) -> Iterator[bool]:
        if self._ingest_lock is None:
            yield True
            return
        try:
            self._ingest_lock.acquire(timeout=0.5)
        except filelock.Timeout:
            yield False
            return
        try:
            yield True
        finally:
            self._ingest_lock.release()

    @contextlib.contextmanager
    def ingesting(self, timeout: float = 600.0) -> Iterator[None]:
        """Hold the machine-wide ingestion turn for another indexing run.

        Waits for the batch in progress (here or in another process) to
        finish, so only one embedding run is in memory at a time.
        """
        if self._ingest_lock is None:
            yield
            return
        self._ingest_lock.acquire(timeout=timeout)
        try:
            yield
        finally:
            self._ingest_lock.release()

    def step(self) -> bool:
        """Do one unit of work; return False when there was nothing to do."""
        with self._folders_lock:
            closing, self._closing = self._closing, []
            tasks, self._tasks = self._tasks, []
        for folder, done in closing:
            try:
                folder.close()
            finally:
                done.set()
        for folder, task, done in tasks:
            try:
                self._guard(folder, task)
            finally:
                done.set()
        now = time.monotonic()
        folders = self.folders()
        for folder in folders:
            try:
                folder.try_become_writer(now)
            except Exception as exc:
                logger.warning("Could not open the index of %s: %s", folder.root, exc)
                folder.error = str(exc)
        writers = [f for f in folders if f.is_writer]
        for folder in writers:
            folder.write_status()
        if self._paused.is_set():
            return False
        for folder in writers:
            subtrees = folder.scan_due(now)
            if subtrees is not None:
                self._guard(folder, folder.scan, subtrees)
                return True
        for folder in writers:
            if folder.has_keyword_work():
                with self._ingesting() as allowed:
                    if allowed:
                        self._guard(folder, folder.keyword_batch)
                return True
        for folder in writers:
            if folder.has_vector_work(now):
                with self._ingesting() as allowed:
                    if allowed:
                        self._guard(folder, folder.vector_window)
                return True
        return False

    @staticmethod
    def _guard(folder: FolderIndex, action: Any, *args: Any) -> None:
        try:
            action(*args)
            folder.error = ""
        except Exception as exc:
            logger.exception("Indexing %s failed", folder.root)
            folder.error = str(exc)
            folder.write_status(force=True)
