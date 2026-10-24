"""Folder mode: search a directory tree directly, its index kept beside it.

A folder's index is an ordinary vault (LanceDB ``docs`` table at the top,
Whoosh under ``fulltext_vault/``, ``vault_metadata.json``), so every segment
that takes a ``vault_path`` works on it unchanged. It normally lives in the
folder itself, in ``<folder>/.tpvault``; a folder that is read-only, on a
network share, or inside a sync client's tree gets one under the app's data
directory instead (``resolve_index_location``).

Indexing runs in the background in two phases. The keyword phase reads and
chunks each file once and writes the chunks to Whoosh, so a folder is
searchable by keyword within minutes. The vector phase reads those stored
chunks back out of Whoosh and embeds them into LanceDB through talkpipe's
``build_rag_database``. Every chunk id is derived from the index and the
file's path relative to the folder, so both stores share ids and redoing a
batch after a crash replaces rows rather than duplicating them.

The per-file state lives in ``manifest.db`` (sqlite), written only by the
process holding ``writer.lock``. Any other process opening the same folder is
a reader: it searches the index and shows the writer's progress from
``status.json``, and takes over (resuming from the manifest) once the writer
exits. One ``FolderScheduler`` thread per process does the work for every
open folder: keywords for all of them before any vectors.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import filelock
from talkpipe import register_segment, register_source, segment, source
from talkpipe.data.extraction import global_extractor_registry
from talkpipe.pipelines.vector_databases import (
    EmbeddingDimensionMismatchError,
    ProcessDocumentsSegment,
    RagIngestError,
    build_rag_database,
)
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshFullTextIndex
from talkpipe.util.data_manipulation import assign_property, extract_property
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.utils.patterns import match_any_paths
from whoosh.query import Prefix

from talkpipe_vault.watchdog import COMMON_IGNORE_PATTERNS

from . import vault_metadata
from .config import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SHINGLE_OVERLAP,
    DEFAULT_SHINGLE_SIZE,
    DEFAULT_VECTOR_TABLE_NAME,
    FOLDER_INDEX_DIRNAME,
    WHOOSH_INDEX_FIELDS,
    get_retrieval_template,
    get_whoosh_index_path,
    resolve_embedding_config,
)

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.db"
STATUS_FILENAME = "status.json"
WRITER_LOCK_FILENAME = "writer.lock"
ROOT_RECORD_FILENAME = "root.json"
EXTERNAL_INDEX_DIRNAME = "folder-indexes"
INGEST_LOCK_FILENAME = "ingest.lock"
MANIFEST_SCHEMA_VERSION = 1

# Files per keyword-phase commit, and chunks per vector-phase embedding run.
KEYWORD_BATCH_FILES = 20
VECTOR_WINDOW_CHUNKS = 1000
# A full rescan catches changes made while no instance ran (at open) and
# events the watcher missed; folders without live events rescan more often.
RESCAN_SECONDS = 30 * 60
UNWATCHED_RESCAN_SECONDS = 10 * 60
# Watcher events are collected for this long before the changed files are
# reconciled, so a burst of saves is handled once.
WATCH_DEBOUNCE_SECONDS = 2.0
WRITER_RETRY_SECONDS = 15.0
HEARTBEAT_SECONDS = 5.0
# A writer whose heartbeat is older than this is shown as stalled; nothing is
# taken over automatically, since the kernel releases the lock of a dead one.
STALLED_SECONDS = 300.0
VECTOR_RETRY_MAX_SECONDS = 300.0

# Filesystem types whose files are reached over the network. An index for a
# folder on one of these is kept on local disk instead of in the folder, so
# no correctness argument ever depends on file locks working across hosts.
NETWORK_FS_TYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "afs",
        "ceph",
        "glusterfs",
        "fuse.sshfs",
        "fuse.rclone",
        "fuse.davfs2",
        "davfs",
    }
)
# Path components that mark a folder synced by a cloud client, and files an
# ancestor of a synced folder holds. An index inside one would be uploaded,
# and downloaded half-written on every other machine.
SYNC_FOLDER_NAMES = frozenset(
    {
        "Dropbox",
        "OneDrive",
        "Google Drive",
        "My Drive",
        "iCloud Drive",
        "Mobile Documents",
        "CloudStorage",
    }
)
SYNC_FOLDER_MARKERS = (".stfolder", ".dropbox")

CACHEDIR_TAG = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# This file is a cache directory tag created by talkpipe-vault.\n"
    "# It holds a search index rebuilt from the documents beside it.\n"
    "# For information about cache directory tags see https://bford.info/cachedir/\n"
)


# --------------------------------------------------------------------------
# Where a folder's index lives


@dataclass(frozen=True)
class IndexLocation:
    """Where a folder's index is kept.

    ``reason`` says why it is outside the folder (empty when it is inside),
    and ``read_only`` is set for an existing index that cannot be written,
    which opens as a reader only.
    """

    path: Path
    in_folder: bool
    read_only: bool = False
    reason: str = ""


def external_index_path(root: Path, base: Path) -> Path:
    """The app-data location of a folder's index, unique per folder path."""
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    name = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in root.name)
    return base / EXTERNAL_INDEX_DIRNAME / f"{name.strip('-._') or 'folder'}-{digest}"


def _mount_type(path: Path) -> str:
    """Filesystem type of the mount holding *path* ("" when unknown)."""
    try:
        with open("/proc/self/mounts", encoding="utf-8") as mounts:
            entries = [line.split() for line in mounts]
    except OSError:
        return ""
    best, fs_type = "", ""
    text = str(path)
    for fields in entries:
        if len(fields) < 3:
            continue
        # Mount points escape spaces as \040 in /proc/self/mounts.
        point = fields[1].replace("\\040", " ")
        inside = text == point or text.startswith(point.rstrip("/") + "/")
        if inside and len(point) > len(best):
            best, fs_type = point, fields[2]
    return fs_type


def _sync_client(root: Path) -> str:
    """Name of the sync folder *root* lies in, or ""."""
    for part in root.parts:
        if part in SYNC_FOLDER_NAMES or part.startswith("OneDrive - "):
            return part
    for ancestor in (root, *root.parents):
        for marker in SYNC_FOLDER_MARKERS:
            if (ancestor / marker).exists():
                return ancestor.name or str(ancestor)
    return ""


def external_reason(root: Path) -> str:
    """Why *root* should not hold its own index, or "" when it may."""
    if _mount_type(root) in NETWORK_FS_TYPES:
        return "the folder is on a network share"
    client = _sync_client(root)
    if client:
        return f"the folder is synced ({client})"
    return ""


def resolve_index_location(
    root: str | Path, external_base: Path, *, force_external: str = ""
) -> IndexLocation:
    """Decide where the index of the folder *root* lives.

    First match wins: an index already in the folder; an index already in
    app data (``external_base``); app data when *force_external* gives a
    reason (a fenced deployment, or the user's choice) or the folder is on a
    network share or synced; otherwise the folder itself, if a ``.tpvault``
    can be created and written there.
    """
    root = Path(os.path.realpath(root))
    in_tree = root / FOLDER_INDEX_DIRNAME
    if in_tree.is_dir():
        writable = os.access(in_tree, os.W_OK)
        return IndexLocation(
            in_tree,
            True,
            read_only=not writable,
            reason="" if writable else "the folder's index is read-only",
        )
    external = external_index_path(root, external_base)
    if external.is_dir():
        return IndexLocation(external, False, reason=_recorded_reason(external))
    reason = force_external or external_reason(root)
    if not reason:
        try:
            in_tree.mkdir(exist_ok=True)
            probe = in_tree / f".probe-{uuid.uuid4().hex}"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return IndexLocation(in_tree, True)
        except OSError:
            with contextlib.suppress(OSError):
                in_tree.rmdir()
            reason = "the folder is not writable"
    return IndexLocation(external, False, reason=reason)


def _recorded_reason(external: Path) -> str:
    try:
        data = json.loads((external / ROOT_RECORD_FILENAME).read_text("utf-8"))
        return str(data.get("reason") or "kept in app data")
    except (OSError, ValueError, AttributeError):
        return "kept in app data"


def _prepare_index_dir(location: IndexLocation, root: Path) -> None:
    """Create the index directory with its hygiene files.

    ``.gitignore`` makes git skip the directory without editing the user's
    own ignore file, and ``CACHEDIR.TAG`` makes borg, restic
    ``--exclude-caches`` and ``tar --exclude-caches`` skip it. An index in
    app data records which folder it belongs to.
    """
    location.path.mkdir(parents=True, exist_ok=True)
    gitignore = location.path / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    tag = location.path / "CACHEDIR.TAG"
    if not tag.exists():
        tag.write_text(CACHEDIR_TAG, encoding="utf-8")
    if not location.in_folder:
        record = location.path / ROOT_RECORD_FILENAME
        record.write_text(
            json.dumps({"root": str(root), "reason": location.reason}, indent=2),
            encoding="utf-8",
        )


# --------------------------------------------------------------------------
# Per-file state


class Manifest:
    """The writer's per-file indexing state, in sqlite.

    ``files`` has one row per document file, keyed by its path relative to
    the folder (``/``-separated), with the stat and content hash it was
    indexed at and its state: ``pending`` (needs keywords), ``keyword``
    (searchable by keyword, needs vectors), ``done``, or ``error``. ``meta``
    holds the index id, the folder it was created for, and the chunking
    settings its chunks were made with.
    """

    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS files ("
                "relpath TEXT PRIMARY KEY, fkey TEXT NOT NULL, size INTEGER, "
                "mtime_ns INTEGER, sha256 TEXT, state TEXT NOT NULL, "
                "error TEXT, n_chunks INTEGER NOT NULL DEFAULT 0)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS files_state ON files (state)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, **values: Any) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                [(key, str(value)) for key, value in values.items()],
            )

    def rows_under(self, subtree: str) -> dict[str, dict[str, Any]]:
        """Rows for the file *subtree* or every file below the directory."""
        query = "SELECT relpath, fkey, size, mtime_ns, sha256, state FROM files"
        params: tuple[Any, ...] = ()
        if subtree:
            # substr rather than LIKE: relpaths may contain % and _.
            query += " WHERE relpath = ? OR substr(relpath, 1, ?) = ?"
            params = (subtree, len(subtree) + 1, subtree + "/")
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return {
            row[0]: {
                "fkey": row[1],
                "size": row[2],
                "mtime_ns": row[3],
                "sha256": row[4],
                "state": row[5],
            }
            for row in rows
        }

    def apply_scan(
        self,
        pending: Iterable[tuple[str, str, int, int, str | None]],
        restat: Iterable[tuple[str, int, int]],
        removed: Iterable[str],
    ) -> None:
        """Record a scan: files to (re)index, files only touched, files gone."""
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO files "
                "(relpath, fkey, size, mtime_ns, sha256, state, error, n_chunks) "
                "VALUES (?, ?, ?, ?, ?, 'pending', NULL, 0)",
                list(pending),
            )
            self._conn.executemany(
                "UPDATE files SET size = ?, mtime_ns = ? WHERE relpath = ?",
                [(size, mtime, rel) for rel, size, mtime in restat],
            )
            self._conn.executemany(
                "DELETE FROM files WHERE relpath = ?", [(rel,) for rel in removed]
            )

    def files_in_state(self, state: str, limit: int | None = None) -> list[dict]:
        query = (
            "SELECT relpath, fkey, sha256, n_chunks FROM files WHERE state = ? "
            "ORDER BY relpath"
        )
        params: tuple[Any, ...] = (state,)
        if limit is not None:
            query += " LIMIT ?"
            params = (state, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"relpath": r[0], "fkey": r[1], "sha256": r[2], "n_chunks": r[3]}
            for r in rows
        ]

    def has_state(self, state: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM files WHERE state = ? LIMIT 1", (state,)
            ).fetchone()
        return row is not None

    def mark_keyword(self, results: Iterable[tuple[str, int, int, int, str]]) -> None:
        """Files whose chunks are committed to Whoosh: (rel, n, size, mtime, sha)."""
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE files SET state = 'keyword', error = NULL, n_chunks = ?, "
                "size = ?, mtime_ns = ?, sha256 = ? WHERE relpath = ?",
                [(n, size, mtime, sha, rel) for rel, n, size, mtime, sha in results],
            )

    def mark_error(self, results: Iterable[tuple[str, str, int, int]]) -> None:
        """Files that failed: (rel, message, size, mtime); retried when changed."""
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE files SET state = 'error', error = ?, size = ?, "
                "mtime_ns = ? WHERE relpath = ?",
                [(msg, size, mtime, rel) for rel, msg, size, mtime in results],
            )

    def mark_done(self, relpaths: Iterable[str]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE files SET state = 'done' WHERE relpath = ? "
                "AND state = 'keyword'",
                [(rel,) for rel in relpaths],
            )

    def remove(self, relpaths: Iterable[str]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "DELETE FROM files WHERE relpath = ?", [(rel,) for rel in relpaths]
            )

    def reset(self, from_states: tuple[str, ...], to_state: str) -> None:
        marks = ",".join("?" for _ in from_states)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE files SET state = ? WHERE state IN ({marks})",  # nosec B608 - placeholders only
                (to_state, *from_states),
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*), SUM(n_chunks = 0), SUM(n_chunks) "
                "FROM files GROUP BY state"
            ).fetchall()
        counts = {state: int(total) for state, total, _, _ in rows}
        indexed = [row for row in rows if row[0] in ("keyword", "done")]
        counts["empty"] = sum(int(row[2] or 0) for row in indexed)
        counts["chunks"] = sum(int(row[3] or 0) for row in indexed)
        return counts


# --------------------------------------------------------------------------
# One folder


def _file_key(relpath: str) -> str:
    return hashlib.sha1(relpath.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_hidden(relpath: str) -> bool:
    return any(part.startswith(".") for part in relpath.split("/"))


def _supported_extensions() -> set[str]:
    # Read on every scan: plugins may register extractors at any time.
    return {ext.lower() for ext in global_extractor_registry.registered_extensions}


class _ChangeHandler(FileSystemEventHandler):
    """Collects the folder-relative paths watchdog reports as changed.

    watchdog's pattern matching looks only at a path's last component, so a
    ``.*`` ignore does not stop events from inside ``.tpvault`` or ``.git``;
    any path with a hidden component is dropped here instead, or the
    writer's own index writes would keep re-triggering it.
    """

    def __init__(self, folder: FolderIndex):
        self.folder = folder

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            if raw:
                self.folder.note_change(os.fsdecode(raw))


class FolderIndex:
    """One folder opened in folder mode: its index, manifest, and writer role.

    Every method that writes (scan, keyword_batch, vector_window, reembed,
    rebuild) is called from the scheduler thread, and only while this
    process is the folder's writer.
    """

    def __init__(
        self,
        root: str | Path,
        location: IndexLocation,
        *,
        embedding_source: str,
        embedding_model: str,
        chunk_size: int,
        shingle_size: int,
        overlap: int,
    ):
        self.root = Path(os.path.realpath(root))
        self.location = location
        self.index_path = str(location.path)
        self.embedding_source = embedding_source
        self.embedding_model = embedding_model
        self.chunk_size = chunk_size
        self.shingle_size = shingle_size
        self.overlap = overlap
        recorded = vault_metadata.load_embedding_config(self.index_path)
        if recorded:
            # The embedder the vectors were made with is authoritative.
            self.embedding_source = str(recorded["source"])
            self.embedding_model = str(recorded["model"])
        self.is_writer = False
        self.manifest: Manifest | None = None
        self.index_uuid = ""
        self.error = ""
        self.vector_error = ""
        self.vectors_paused = False
        self.watch_mode = ""
        self.chunks_skipped = 0
        self._lock = (
            None
            if location.read_only
            else filelock.FileLock(
                str(location.path / WRITER_LOCK_FILENAME), is_singleton=False
            )
        )
        self._next_lock_try = 0.0
        self._vector_retry_at = 0.0
        self._vector_failures = 0
        self._needs_preflight = True
        self._last_scan = 0.0
        self._last_status_write = 0.0
        self._scanning = False
        self._dirty: set[str] = set()
        self._last_change = 0.0
        self._dirty_lock = threading.Lock()
        self._observer: Any = None

    # -- roles -------------------------------------------------------------

    def try_become_writer(self, now: float | None = None, watch: bool = True) -> bool:
        """Take the writer lock if no other process holds it.

        With *watch*, a writer also watches the folder for changes (a
        one-shot indexing run passes False).
        """
        now = time.monotonic() if now is None else now
        if self.is_writer or self._lock is None or now < self._next_lock_try:
            return self.is_writer
        self._next_lock_try = now + WRITER_RETRY_SECONDS
        try:
            self._lock.acquire(timeout=0)
        except filelock.Timeout:
            return False
        try:
            _prepare_index_dir(self.location, self.root)
            self.manifest = Manifest(self.location.path / MANIFEST_FILENAME)
            self._init_meta()
        except Exception:
            self.manifest = None
            self._lock.release()
            raise
        self.is_writer = True
        self._last_scan = 0.0
        if watch:
            self._start_watching()
        self.write_status(force=True)
        return True

    def _init_meta(self) -> None:
        assert self.manifest is not None  # nosec B101 - set by the caller
        manifest = self.manifest
        self.index_uuid = manifest.get_meta("index_uuid") or ""
        if not self.index_uuid:
            self.index_uuid = uuid.uuid4().hex
            manifest.set_meta(
                index_uuid=self.index_uuid,
                schema_version=MANIFEST_SCHEMA_VERSION,
                chunk_size=self.chunk_size,
                shingle_size=self.shingle_size,
                overlap=self.overlap,
            )
        else:
            # Chunks already in the index were made with the recorded
            # settings; new ones must match them (Rebuild applies new ones).
            self.chunk_size = int(manifest.get_meta("chunk_size") or self.chunk_size)
            self.shingle_size = int(
                manifest.get_meta("shingle_size") or self.shingle_size
            )
            self.overlap = int(manifest.get_meta("overlap") or self.overlap)
        self.chunks_skipped = int(manifest.get_meta("chunks_skipped") or 0)
        # The folder may have moved since it was indexed; sources are rebased
        # from the recorded root at query time.
        if not manifest.get_meta("root"):
            manifest.set_meta(root=self.root)

    def close(self) -> None:
        """Stop watching and give up the writer role (another process may take it)."""
        self._stop_watching()
        if self.manifest is not None:
            self.write_status(force=True, phase="stopped")
            self.manifest.close()
            self.manifest = None
        if self._lock is not None and self.is_writer:
            self._lock.release()
        self.is_writer = False

    @property
    def recorded_root(self) -> str:
        """The folder path the stored chunk sources were written under."""
        if self.manifest is not None:
            return self.manifest.get_meta("root") or str(self.root)
        return str(self._read_status().get("recorded_root") or self.root)

    # -- watching ----------------------------------------------------------

    def _start_watching(self) -> None:
        if _mount_type(self.root) in NETWORK_FS_TYPES or not os.access(
            self.root, os.W_OK
        ):
            # No reliable change events: rely on periodic rescans.
            self.watch_mode = "rescan"
            return
        try:
            observer = Observer()
            observer.schedule(_ChangeHandler(self), str(self.root), recursive=True)
            observer.daemon = True
            observer.start()
        except Exception as exc:
            # inotify watch limits, unsupported filesystems, and the like.
            logger.warning(
                "Watching %s failed (%s); rescanning instead", self.root, exc
            )
            self.watch_mode = "rescan"
            return
        self._observer = observer
        self.watch_mode = "live"

    def _stop_watching(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            with contextlib.suppress(Exception):
                observer.stop()
                observer.join(timeout=5)

    def note_change(self, path: str) -> None:
        """Record that *path* changed; it is reconciled after a short delay."""
        try:
            rel = Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return
        if rel in ("", ".") or _is_hidden(rel):
            return
        if not match_any_paths(
            [path], excluded_patterns=COMMON_IGNORE_PATTERNS, case_sensitive=False
        ):
            return
        with self._dirty_lock:
            self._dirty.add(rel)
            self._last_change = time.monotonic()

    # -- scanning ----------------------------------------------------------

    def scan_due(self, now: float) -> bool:
        interval = (
            RESCAN_SECONDS if self.watch_mode == "live" else UNWATCHED_RESCAN_SECONDS
        )
        if not self._last_scan or now - self._last_scan >= interval:
            return True
        with self._dirty_lock:
            return (
                bool(self._dirty) and now - self._last_change >= WATCH_DEBOUNCE_SECONDS
            )

    def run_due_scan(self, now: float) -> None:
        """Rescan everything when due, else only what the watcher reported."""
        interval = (
            RESCAN_SECONDS if self.watch_mode == "live" else UNWATCHED_RESCAN_SECONDS
        )
        with self._dirty_lock:
            dirty, self._dirty = self._dirty, set()
        if not self._last_scan or now - self._last_scan >= interval:
            self.scan()
            self._last_scan = now
        else:
            self.scan(sorted(dirty))

    def _disk_files(
        self, subtree: str, extensions: set[str]
    ) -> dict[str, os.stat_result]:
        """Supported, visible document files at or below *subtree*."""
        found: dict[str, os.stat_result] = {}
        start = self.root / subtree if subtree else self.root

        def consider(path: Path) -> None:
            rel = path.relative_to(self.root).as_posix()
            if _is_hidden(rel) or path.suffix[1:].lower() not in extensions:
                return
            if not match_any_paths(
                [str(path)],
                excluded_patterns=COMMON_IGNORE_PATTERNS,
                case_sensitive=False,
            ):
                return
            try:
                if path.is_symlink():
                    target = os.path.realpath(path)
                    if not target.startswith(str(self.root) + os.sep):
                        return
                stat = path.stat()
            except OSError:
                return
            found[rel] = stat

        if start.is_file():
            consider(start)
        elif start.is_dir() and not start.is_symlink():
            for dirpath, dirnames, filenames in os.walk(start):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if not name.startswith("."):
                        consider(Path(dirpath) / name)
        return found

    def scan(self, subtrees: Iterable[str] | None = None) -> None:
        """Reconcile the manifest with the files on disk.

        New files become pending. A file whose size or modification time
        changed is hashed: unchanged content only updates the stat (a
        ``touch`` or a branch checkout costs no re-embedding); changed
        content, and every deleted file, has its chunks removed from both
        stores at once, so stale text is never served, and is then pending
        again (or forgotten).
        """
        if self.manifest is None:
            return
        extensions = _supported_extensions()
        roots = [""] if subtrees is None else list(subtrees)
        # A subtree inside another one is covered by it.
        roots = [r for r in roots if not any(o != r and _within(r, o) for o in roots)]
        self._scanning = True
        try:
            for subtree in roots:
                self._scan_subtree(subtree, extensions)
        finally:
            self._scanning = False
        self.write_status(force=True)

    def _scan_subtree(self, subtree: str, extensions: set[str]) -> None:
        assert self.manifest is not None  # nosec B101 - checked by scan()
        known = self.manifest.rows_under(subtree)
        on_disk = self._disk_files(subtree, extensions)
        pending: list[tuple[str, str, int, int, str | None]] = []
        restat: list[tuple[str, int, int]] = []
        stale_keys: list[str] = []
        for rel, stat in on_disk.items():
            row = known.get(rel)
            if row is None:
                pending.append(
                    (rel, _file_key(rel), stat.st_size, stat.st_mtime_ns, None)
                )
                continue
            if row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
                continue
            digest = None
            with contextlib.suppress(OSError):
                digest = _sha256(self.root / rel)
            if digest and digest == row["sha256"] and row["state"] != "error":
                restat.append((rel, stat.st_size, stat.st_mtime_ns))
                continue
            stale_keys.append(row["fkey"])
            pending.append((rel, row["fkey"], stat.st_size, stat.st_mtime_ns, digest))
        removed = [rel for rel in known if rel not in on_disk]
        stale_keys.extend(known[rel]["fkey"] for rel in removed)
        if stale_keys:
            self._delete_chunks(stale_keys)
        self.manifest.apply_scan(pending, restat, removed)

    def _file_prefix(self, fkey: str) -> str:
        return f"{self.index_uuid[:8]}-{fkey}:"

    def _delete_chunks(self, fkeys: list[str]) -> None:
        """Remove every chunk of the given files from Whoosh and LanceDB."""
        prefixes = [self._file_prefix(fkey) for fkey in fkeys]
        whoosh_path = get_whoosh_index_path(self.index_path)
        if os.path.isdir(whoosh_path):
            with (
                WhooshFullTextIndex(whoosh_path, fields=WHOOSH_INDEX_FIELDS) as ix,
                ix.ix.writer() as writer,
            ):
                for prefix in prefixes:
                    writer.delete_by_query(Prefix("doc_id", prefix))
        table = self._docs_table()
        if table is None:
            return
        # Prefixes are hex plus "-" and ":", so they are safe in a predicate
        # and hold none of LIKE's wildcards.
        for start in range(0, len(prefixes), 50):
            predicate = " OR ".join(
                f"id LIKE '{prefix}%'" for prefix in prefixes[start : start + 50]
            )
            table.delete(predicate)

    def _docs_table(self) -> Any:
        store = LanceDBDocumentStore(
            path=self.index_path, table_name=DEFAULT_VECTOR_TABLE_NAME
        )
        try:
            table, _ = store._get_table()
        except ValueError:
            return None
        return table

    # -- keyword phase -----------------------------------------------------

    def has_keyword_work(self) -> bool:
        return self.manifest is not None and self.manifest.has_state("pending")

    def keyword_batch(self, limit: int = KEYWORD_BATCH_FILES) -> int:
        """Chunk up to *limit* pending files into Whoosh; return files handled.

        The chunks of each file replace any it already had (a crash after
        the commit but before the manifest update redoes the file without
        duplicating it), and the manifest is updated only after the commit.
        """
        if self.manifest is None:
            return 0
        rows = self.manifest.files_in_state("pending", limit)
        if not rows:
            return 0
        processor = ProcessDocumentsSegment(
            chunk_size=self.chunk_size,
            shingle_size=self.shingle_size,
            overlap=self.overlap,
        )
        indexed: list[tuple[str, int, int, int, str]] = []
        failed: list[tuple[str, str, int, int]] = []
        gone: list[str] = []
        with (
            WhooshFullTextIndex(
                get_whoosh_index_path(self.index_path), fields=WHOOSH_INDEX_FIELDS
            ) as ix,
            ix.ix.writer() as writer,
        ):
            for row in rows:
                rel = row["relpath"]
                path = self.root / rel
                prefix = self._file_prefix(row["fkey"])
                try:
                    stat = path.stat()
                    digest = row["sha256"] or _sha256(path)
                except OSError:
                    gone.append(rel)
                    continue
                writer.delete_by_query(Prefix("doc_id", prefix))
                count = 0
                try:
                    # listFiles globs its input: escape names like "a [1].pdf".
                    for chunk in processor.transform([glob.escape(str(path))]):
                        writer.add_document(
                            doc_id=f"{prefix}{count:05d}",
                            content=str(chunk.get("content", "")),
                            path=str(path),
                            filename=str(chunk.get("title") or path.name),
                        )
                        count += 1
                except Exception as exc:
                    logger.warning("Could not index %s: %s", path, exc)
                    failed.append((rel, str(exc), stat.st_size, stat.st_mtime_ns))
                    continue
                indexed.append((rel, count, stat.st_size, stat.st_mtime_ns, digest))
        self.manifest.mark_keyword(indexed)
        self.manifest.mark_error(failed)
        self.manifest.remove(gone)
        self.write_status(force=True)
        return len(rows)

    # -- vector phase ------------------------------------------------------

    def has_vector_work(self, now: float) -> bool:
        return (
            self.manifest is not None
            and not self.vectors_paused
            and now >= self._vector_retry_at
            and self.manifest.has_state("keyword")
        )

    def _window(self) -> tuple[list[str], list[dict[str, Any]]]:
        """Whole files totalling about VECTOR_WINDOW_CHUNKS stored chunks."""
        assert self.manifest is not None  # nosec B101 - checked by the caller
        files: list[str] = []
        chunks: list[dict[str, Any]] = []
        candidates = self.manifest.files_in_state("keyword", 500)
        with (
            WhooshFullTextIndex(get_whoosh_index_path(self.index_path)) as ix,
            ix.ix.searcher() as searcher,
        ):
            for row in candidates:
                if files and len(chunks) + row["n_chunks"] > VECTOR_WINDOW_CHUNKS:
                    break
                files.append(row["relpath"])
                if not row["n_chunks"]:
                    continue
                hits = searcher.search(
                    Prefix("doc_id", self._file_prefix(row["fkey"])), limit=None
                )
                # Shaped like ProcessDocumentsSegment output, so the stored
                # rows match a named vault's.
                chunks.extend(
                    {
                        "id": hit["doc_id"],
                        "content": hit.get("content", ""),
                        "shingle_text": hit.get("content", ""),
                        "source": hit.get("path", ""),
                        "title": hit.get("filename", ""),
                    }
                    for hit in hits
                )
        return files, chunks

    def vector_window(self) -> int:
        """Embed one window of keyword-indexed files; return chunks embedded."""
        if self.manifest is None:
            return 0
        files, chunks = self._window()
        if not chunks:
            self.manifest.mark_done(files)
            return 0
        recorded = vault_metadata.load_embedding_config(self.index_path)
        if recorded is None:
            # Recorded before any vector exists, so a reader (or the next
            # writer) queries with the model these vectors are made with.
            self._record_embedder()
            recorded = vault_metadata.load_embedding_config(self.index_path) or {}
        try:
            result = build_rag_database(
                None,
                path=self.index_path,
                embedding_model=self.embedding_model,
                embedding_source=self.embedding_source,
                table_name=DEFAULT_VECTOR_TABLE_NAME,
                doc_id_field="id",
                batch_size=100,
                expected_dimension=recorded.get("dimension"),
                preflight=self._needs_preflight,
                chunks=chunks,
            )
        except EmbeddingDimensionMismatchError as exc:
            self.vectors_paused = True
            self.vector_error = (
                f"This folder's vectors were made with {exc.expected}-dimensional "
                f"embeddings, but {self.embedding_source}/{self.embedding_model} "
                f"produces {exc.actual}; re-embed the folder to use it."
            )
            self.write_status(force=True)
            return 0
        except RagIngestError as exc:
            self._vector_failures += 1
            delay = min(
                VECTOR_RETRY_MAX_SECONDS, 15.0 * 2 ** (self._vector_failures - 1)
            )
            self._vector_retry_at = time.monotonic() + delay
            self._needs_preflight = True
            self.vector_error = f"{exc} Retrying in {int(delay)} s."
            self.write_status(force=True)
            return 0
        # Only now: build_rag_database has flushed and optimized the table.
        self.manifest.mark_done(files)
        self._needs_preflight = False
        self._vector_failures = 0
        self.vector_error = ""
        if result.chunks_skipped:
            self.chunks_skipped += result.chunks_skipped
            self.manifest.set_meta(chunks_skipped=self.chunks_skipped)
        self.write_status(force=True)
        return result.chunks_indexed

    def _record_embedder(self) -> None:
        try:
            template = get_retrieval_template(self.embedding_model)
        except Exception:  # pragma: no cover - defensive; config read is cheap
            template = None
        vault_metadata.record_embedding_config(
            self.index_path,
            source=self.embedding_source,
            model=self.embedding_model,
            dimension=vault_metadata.probe_embedding_dimension(
                self.embedding_source, self.embedding_model
            ),
            retrieval_template=template,
        )

    # -- maintenance -------------------------------------------------------

    def reembed(self, embedding_source: str, embedding_model: str) -> None:
        """Replace the vectors with ones from another embedder.

        Keyword search keeps answering throughout; vector search for this
        folder degrades to the partial-index behaviour until it catches up.
        """
        if self.manifest is None:
            raise RuntimeError("only the folder's indexing process can re-embed it")
        self._drop_docs_table()
        self.embedding_source = embedding_source
        self.embedding_model = embedding_model
        self._record_embedder()
        self.manifest.reset(("done",), "keyword")
        self.vectors_paused = False
        self.vector_error = ""
        self._needs_preflight = True
        self._vector_retry_at = 0.0
        self.write_status(force=True)

    def rebuild(self, chunk_size: int, shingle_size: int, overlap: int) -> None:
        """Re-chunk and re-index every file with new chunking settings."""
        if self.manifest is None:
            raise RuntimeError("only the folder's indexing process can rebuild it")
        self._drop_docs_table()
        whoosh_path = get_whoosh_index_path(self.index_path)
        if os.path.isdir(whoosh_path):
            shutil.rmtree(whoosh_path)
        self.chunk_size, self.shingle_size, self.overlap = (
            chunk_size,
            shingle_size,
            overlap,
        )
        self.manifest.set_meta(
            chunk_size=chunk_size, shingle_size=shingle_size, overlap=overlap
        )
        self.manifest.reset(("keyword", "done", "error"), "pending")
        self.vectors_paused = False
        self.vector_error = ""
        self.write_status(force=True)

    def _drop_docs_table(self) -> None:
        store = LanceDBDocumentStore(
            path=self.index_path, table_name=DEFAULT_VECTOR_TABLE_NAME
        )
        with contextlib.suppress(FileNotFoundError, ValueError):
            store._get_db().drop_table(DEFAULT_VECTOR_TABLE_NAME)

    # -- status ------------------------------------------------------------

    def _read_status(self) -> dict[str, Any]:
        try:
            data = json.loads(
                (self.location.path / STATUS_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _counts(self) -> dict[str, int]:
        assert self.manifest is not None  # nosec B101 - writer only
        counts = self.manifest.counts()
        keyword, done = counts.get("keyword", 0), counts.get("done", 0)
        pending, failed = counts.get("pending", 0), counts.get("error", 0)
        return {
            "files_total": keyword + done + pending + failed,
            "files_keyword": keyword + done,
            "files_embedded": done,
            "files_pending": pending,
            "files_error": failed,
            "files_empty": counts.get("empty", 0),
            "chunks": counts.get("chunks", 0),
        }

    def _phase(self, counts: dict[str, int]) -> str:
        if self._scanning:
            return "scanning"
        if counts["files_pending"]:
            return "keywords"
        if counts["files_keyword"] > counts["files_embedded"]:
            return "paused" if self.vectors_paused else "meaning"
        return "ready"

    def write_status(self, force: bool = False, phase: str = "") -> None:
        """Publish progress for readers (and for this process's own pages)."""
        now = time.monotonic()
        if self.manifest is None or (
            not force and now - self._last_status_write < HEARTBEAT_SECONDS
        ):
            return
        counts = self._counts()
        payload = {
            "index_uuid": self.index_uuid,
            "root": str(self.root),
            "recorded_root": self.recorded_root,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "heartbeat": time.time(),
            "phase": phase or self._phase(counts),
            "embedding": f"{self.embedding_source}/{self.embedding_model}",
            "vector_error": self.vector_error,
            "error": self.error,
            "watch_mode": self.watch_mode,
            "chunks_skipped": self.chunks_skipped,
            **counts,
        }
        path = self.location.path / STATUS_FILENAME
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Could not write %s: %s", path, exc)
        self._last_status_write = now

    def status(self) -> dict[str, Any]:
        """Progress and role, for the folder list and the partial-index notes."""
        if self.is_writer and self.manifest is not None:
            counts = self._counts()
            data: dict[str, Any] = {
                **counts,
                "phase": self._phase(counts),
                "role": "writer",
                "writer": "this window",
                "stalled": False,
                "vector_error": self.vector_error,
                "error": self.error,
                "watch_mode": self.watch_mode,
                "embedding": f"{self.embedding_source}/{self.embedding_model}",
            }
        else:
            recorded = self._read_status()
            heartbeat = float(recorded.get("heartbeat") or 0)
            phase = str(recorded.get("phase") or "waiting")
            stalled = (
                phase not in ("stopped", "ready")
                and heartbeat > 0
                and time.time() - heartbeat > STALLED_SECONDS
            )
            if self.location.read_only:
                writer = "nobody (read-only index)"
            elif phase == "stopped" or not recorded:
                writer = "nobody yet"
            else:
                writer = (
                    f"another vault process (pid {recorded.get('pid')} on "
                    f"{recorded.get('host')})"
                )
            data = {
                key: int(recorded.get(key) or 0)
                for key in (
                    "files_total",
                    "files_keyword",
                    "files_embedded",
                    "files_pending",
                    "files_error",
                    "files_empty",
                    "chunks",
                )
            }
            data.update(
                phase="stalled" if stalled else phase,
                role="read-only" if self.location.read_only else "reader",
                writer=writer,
                stalled=stalled,
                vector_error=str(recorded.get("vector_error") or ""),
                error=str(recorded.get("error") or ""),
                watch_mode=str(recorded.get("watch_mode") or ""),
                embedding=str(recorded.get("embedding") or ""),
            )
        total, embedded = data["files_total"], data["files_embedded"]
        data.update(
            root=str(self.root),
            name=self.root.name or str(self.root),
            index_path=self.index_path,
            in_folder=self.location.in_folder,
            location_reason=self.location.reason,
            keyword_coverage=(data["files_keyword"] / total) if total else 0.0,
            vector_coverage=(embedded / total) if total else 0.0,
            complete=bool(total) and embedded == total,
        )
        return data


def delete_folder_index(folder: FolderIndex) -> str:
    """Delete a closed folder's index (never its documents).

    Returns "" when deleted, else why not: only a folder-mode index
    directory is ever removed, and not while another process indexes it.
    """
    path = folder.location.path
    if folder.is_writer:
        return "the folder is still open"
    if path.name != FOLDER_INDEX_DIRNAME and path.parent.name != EXTERNAL_INDEX_DIRNAME:
        return f"{path} is not a folder index"
    if folder.location.read_only:
        return "the index is read-only"
    if not path.is_dir():
        return ""
    lock = filelock.FileLock(str(path / WRITER_LOCK_FILENAME), is_singleton=False)
    try:
        lock.acquire(timeout=0)
    except filelock.Timeout:
        return "another vault process is indexing this folder"
    try:
        shutil.rmtree(path)
    finally:
        lock.release()
    return ""


def _within(path: str, ancestor: str) -> bool:
    return ancestor == "" or path == ancestor or path.startswith(ancestor + "/")


# --------------------------------------------------------------------------
# The background worker


class FolderScheduler:
    """One background thread doing the indexing for every open folder.

    Keyword work for all folders comes before any vector work, and changed
    files go through the keyword phase before any vector backlog. A
    machine-wide ``ingest.lock`` is held for each batch, so two vault
    processes interleave instead of both reaching ingestion's memory peak at
    once.
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
            if folder.scan_due(now):
                self._guard(folder, folder.run_due_scan, now)
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


# --------------------------------------------------------------------------
# Opening a folder, and the segments


INDEX_LOCATION_AUTO = "auto"
INDEX_LOCATION_HOME = "home"


def default_index_home() -> Path:
    """Where indexes kept outside their folders go: the vault root when one
    is configured (TALKPIPE_VAULT_ROOT), else the vault home directory."""
    from talkpipe_vault.apps import access_control, user_settings

    return access_control.vault_root() or user_settings.get_vault_home()


def open_folder(
    root: str | Path,
    *,
    embedding_source: str | None = None,
    embedding_model: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    shingle_size: int = DEFAULT_SHINGLE_SIZE,
    overlap: int = DEFAULT_SHINGLE_OVERLAP,
    index_location: str = INDEX_LOCATION_AUTO,
    index_home: str | Path | None = None,
) -> FolderIndex:
    """Open the folder *root* for folder mode, deciding where its index lives.

    The embedder given (else the configured one) is used only for a folder
    that has no vectors yet; one already indexed keeps the embedder its
    vectors were made with. *index_location* "home" keeps a new index in
    app data instead of the folder; so does a configured vault root, since
    an operator who fenced where vault data may live wants it there.
    """
    from talkpipe_vault.apps import access_control

    path = Path(os.path.realpath(os.path.expanduser(os.fspath(root))))
    if not path.is_dir():
        raise FileNotFoundError(f"{path} is not a folder")
    force = ""
    if access_control.vault_root() is not None:
        force = "this server keeps indexes under its vault root"
    elif index_location == INDEX_LOCATION_HOME:
        force = "indexes are kept in app data (your setting)"
    base = Path(index_home).expanduser() if index_home else default_index_home()
    location = resolve_index_location(path, base, force_external=force)
    embedding_model, embedding_source = resolve_embedding_config(
        embedding_model, embedding_source
    )
    return FolderIndex(
        path,
        location,
        embedding_source=embedding_source,
        embedding_model=embedding_model,
        chunk_size=chunk_size,
        shingle_size=shingle_size,
        overlap=overlap,
    )


def find_index_path(
    root: str | Path, index_home: str | Path | None = None
) -> Path | None:
    """The existing index of the folder *root*, or None; never creates one."""
    path = Path(os.path.realpath(os.path.expanduser(os.fspath(root))))
    in_tree = path / FOLDER_INDEX_DIRNAME
    if in_tree.is_dir():
        return in_tree
    base = Path(index_home).expanduser() if index_home else default_index_home()
    external = external_index_path(path, base)
    return external if external.is_dir() else None


def index_folder_root(index_path: str | Path) -> Path | None:
    """The folder a folder-mode index belongs to, or None for a named vault."""
    index = Path(index_path)
    if index.name == FOLDER_INDEX_DIRNAME:
        return index.parent
    try:
        data = json.loads((index / ROOT_RECORD_FILENAME).read_text("utf-8"))
        return Path(data["root"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def read_index_status(index_path: str | Path) -> dict[str, Any]:
    """The writer's last published status of a folder index ({} if none)."""
    try:
        data = json.loads((Path(index_path) / STATUS_FILENAME).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def vectors_complete(index_path: str | Path) -> bool:
    """False while a folder index still has files without vectors.

    A named vault (no folder status) always counts as complete.
    """
    if index_folder_root(index_path) is None:
        return True
    status = read_index_status(index_path)
    total = int(status.get("files_total") or 0)
    return total > 0 and int(status.get("files_embedded") or 0) >= total


def index_folder_now(folder: FolderIndex, vectors: bool = True) -> dict[str, Any]:
    """Bring *folder* fully up to date in this thread; return its status.

    Scans, keyword-indexes every pending file, then (with *vectors*) embeds
    them. When another process is the folder's writer, nothing is done and
    the status says so; a vector failure ends the run with the error in the
    status, keyword search still complete.
    """
    if not folder.try_become_writer(watch=False):
        return folder.status()
    try:
        folder.scan()
        while folder.has_keyword_work():
            folder.keyword_batch()
        if vectors:
            while folder.has_vector_work(time.monotonic()):
                folder.vector_window()
        return folder.status()
    finally:
        folder.close()


def _split_paths(paths: str | Iterable[str]) -> list[str]:
    if isinstance(paths, str):
        parts = paths.replace(os.pathsep, ",").split(",")
    else:
        parts = list(paths)
    return [p.strip() for p in parts if p and p.strip()]


@register_segment("indexFolder")
@segment()
def index_folder(
    items: Annotated[Iterable[Any], "Folder paths, or items holding one in field"],
    field: Annotated[
        str | None, "Field holding the folder path; None uses the item"
    ] = None,
    set_as: Annotated[str | None, "Field to set the status on; None emits it"] = None,
    embedding_source: Annotated[
        str | None, "Embedding provider for a folder with no vectors yet"
    ] = None,
    embedding_model: Annotated[
        str | None, "Embedding model for a folder with no vectors yet"
    ] = None,
    chunk_size: Annotated[int, "Chunk size for a new index"] = DEFAULT_CHUNK_SIZE,
    shingle_size: Annotated[int, "Shingle size for a new index"] = DEFAULT_SHINGLE_SIZE,
    overlap: Annotated[
        int, "Shingle overlap for a new index"
    ] = DEFAULT_SHINGLE_OVERLAP,
    index_location: Annotated[
        str, "'auto' (in the folder when possible) or 'home' (app data)"
    ] = INDEX_LOCATION_AUTO,
    index_home: Annotated[
        str | None, "Directory for indexes kept outside their folders"
    ] = None,
    vectors: Annotated[bool, "If False, build only the keyword index"] = True,
) -> Iterator[Any]:
    """Index each folder (its whole tree) for folder-mode search, then emit its status.

    Expects folder paths (strings), or dict items holding one in a field.
    Each folder's index is kept in ``<folder>/.tpvault`` (or in app data;
    see ``open_folder``) and brought up to date: new and changed files are
    indexed, deleted ones removed, keywords first and then vectors. Running
    it again only processes what changed. When another process is already
    indexing the folder, this one leaves it alone.

    Emits a status dict per folder (or sets it on the input item): file
    counts (files_total, files_keyword, files_embedded, files_error, ...),
    phase, role, and any vector_error.
    """
    for item in items:
        root = item if field is None else extract_property(item, field)
        folder = open_folder(
            str(root),
            embedding_source=embedding_source,
            embedding_model=embedding_model,
            chunk_size=chunk_size,
            shingle_size=shingle_size,
            overlap=overlap,
            index_location=index_location,
            index_home=index_home,
        )
        status = index_folder_now(folder, vectors=vectors)
        if set_as is None:
            yield status
        else:
            assign_property(item, set_as, status)
            yield item


@register_source("watchFolders")
@source()
def watch_folders(
    paths: Annotated[str, "Folders to keep indexed, comma or os.pathsep separated"],
    embedding_source: Annotated[
        str | None, "Embedding provider for a folder with no vectors yet"
    ] = None,
    embedding_model: Annotated[
        str | None, "Embedding model for a folder with no vectors yet"
    ] = None,
    chunk_size: Annotated[int, "Chunk size for a new index"] = DEFAULT_CHUNK_SIZE,
    shingle_size: Annotated[int, "Shingle size for a new index"] = DEFAULT_SHINGLE_SIZE,
    overlap: Annotated[
        int, "Shingle overlap for a new index"
    ] = DEFAULT_SHINGLE_OVERLAP,
    index_location: Annotated[
        str, "'auto' (in the folder when possible) or 'home' (app data)"
    ] = INDEX_LOCATION_AUTO,
    index_home: Annotated[
        str | None, "Directory for indexes kept outside their folders"
    ] = None,
    interval: Annotated[float, "Seconds between status checks"] = 2.0,
    max_events: Annotated[int | None, "Stop after this many status changes"] = None,
) -> Iterator[dict[str, Any]]:
    """Keep folders indexed in the background and emit their status as it changes.

    Opens each folder for folder mode and runs the same background indexer
    the vault's interfaces use: it becomes each folder's writer when no
    other process is, indexes keywords then vectors, watches for changes
    (rescanning periodically where events are unreliable), and resumes
    where it left off when restarted. Runs until the requested number of
    status changes has been emitted, or forever.

    Emits a status dict (see indexFolder) for a folder whenever its phase
    or file counts change.
    """
    scheduler = FolderScheduler(
        ingest_lock_dir=Path(index_home).expanduser()
        if index_home
        else default_index_home()
    )
    for root in _split_paths(paths):
        scheduler.add(
            open_folder(
                root,
                embedding_source=embedding_source,
                embedding_model=embedding_model,
                chunk_size=chunk_size,
                shingle_size=shingle_size,
                overlap=overlap,
                index_location=index_location,
                index_home=index_home,
            )
        )
    scheduler.start()
    keys = (
        "phase",
        "role",
        "files_total",
        "files_keyword",
        "files_embedded",
        "files_error",
        "vector_error",
        "error",
    )
    last: dict[str, tuple[Any, ...]] = {}
    emitted = 0
    try:
        while max_events is None or emitted < max_events:
            for folder in scheduler.folders():
                status = folder.status()
                signature = tuple(status.get(key) for key in keys)
                if last.get(status["root"]) != signature:
                    last[status["root"]] = signature
                    yield status
                    emitted += 1
                    if max_events is not None and emitted >= max_events:
                        return
            time.sleep(interval)
    finally:
        scheduler.stop()
