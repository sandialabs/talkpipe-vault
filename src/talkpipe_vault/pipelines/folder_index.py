"""Folder mode: search a directory tree directly, its index kept beside it.

A folder's index is an ordinary vault (LanceDB ``docs`` table at the top,
Whoosh under ``fulltext_vault/``, ``vault_metadata.json``), so every segment
that takes a ``vault_path`` works on it unchanged. It normally lives in the
folder itself, in ``<folder>/.tpvault``; a folder that is read-only, on a
network share, or inside a sync client's tree gets one under the app's data
directory instead (``folder_files.resolve_index_location``).

Indexing runs in the background in two phases. The keyword phase reads and
chunks each file once and writes the chunks to Whoosh, so a folder is
searchable by keyword within minutes. The vector phase reads those stored
chunks back out of Whoosh and embeds them into LanceDB through talkpipe's
``build_rag_database``. Every chunk id is derived from the index and the
file's path relative to the folder, so both stores share ids and redoing a
batch after a crash replaces rows rather than duplicating them.

The per-file state lives in ``manifest.db`` (``folder_manifest``), written
only by the process holding ``writer.lock``. Any other process opening the
same folder is a reader: it searches the index and shows the writer's
progress from ``status.json``, and takes over (resuming from the manifest)
once the writer exits. One ``FolderScheduler`` thread per process
(``folder_scheduler``) does the work for every open folder: keywords for all
of them before any vectors.
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
import time
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Annotated, Any

import filelock
from talkpipe import register_segment, register_source, segment, source
from talkpipe.pipelines.vector_databases import (
    EmbeddingDimensionMismatchError,
    ProcessDocumentsSegment,
    RagIngestError,
    build_rag_database,
)
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshFullTextIndex
from talkpipe.util.data_manipulation import assign_property, extract_property
from whoosh.query import Prefix

from . import vault_metadata
from .config import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SHINGLE_OVERLAP,
    DEFAULT_SHINGLE_SIZE,
    DEFAULT_VECTOR_TABLE_NAME,
    FOLDER_INDEX_DIRNAME,
    WHOOSH_INDEX_FIELDS,
    WRITER_LOCK_FILENAME,
    get_retrieval_template,
    get_whoosh_index_path,
    resolve_embedding_config,
)
from .folder_files import (
    EXTERNAL_INDEX_DIRNAME,
    ROOT_RECORD_FILENAME,
    ChangeTracker,
    IndexLocation,
    covering_subtrees,
    diff_files,
    external_index_path,
    prepare_index_dir,
    resolve_index_location,
    sha256_file,
    supported_extensions,
)
from .folder_manifest import MANIFEST_FILENAME, MANIFEST_SCHEMA_VERSION, Manifest
from .folder_scheduler import FolderScheduler

logger = logging.getLogger(__name__)

STATUS_FILENAME = "status.json"

# Files per keyword-phase commit, and chunks per vector-phase embedding run.
KEYWORD_BATCH_FILES = 20
VECTOR_WINDOW_CHUNKS = 1000
WRITER_RETRY_SECONDS = 15.0
HEARTBEAT_SECONDS = 5.0
# A writer whose heartbeat is older than this is shown as stalled; nothing is
# taken over automatically, since the kernel releases the lock of a dead one.
STALLED_SECONDS = 300.0
VECTOR_RETRY_MAX_SECONDS = 300.0


def file_key(relpath: str) -> str:
    """The stable part of a file's chunk ids, from its path in the folder."""
    return hashlib.sha1(relpath.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


# --------------------------------------------------------------------------
# One folder


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
        self.chunks_skipped = 0
        self.changes = ChangeTracker(self.root)
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
        self._last_status_write = 0.0
        self._scanning = False

    @property
    def watch_mode(self) -> str:
        """ "live" with change events, "rescan" without, "" before watching."""
        return self.changes.watch_mode

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
            prepare_index_dir(self.location, self.root)
            self.manifest = Manifest(self.location.path / MANIFEST_FILENAME)
            self._init_meta()
        except Exception:
            self.manifest = None
            self._lock.release()
            raise
        self.is_writer = True
        self.changes.reset()
        if watch:
            self.changes.start_watching()
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
        self.changes.stop_watching()
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

    # -- scanning ----------------------------------------------------------

    def note_change(self, path: str) -> None:
        """Record that *path* changed; it is reconciled after a short delay."""
        self.changes.note_change(path)

    def scan_due(self, now: float) -> list[str] | None:
        """The subtrees to scan now ([""] is the whole folder), or None."""
        return self.changes.due(now)

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
        extensions = supported_extensions()
        roots = covering_subtrees([""] if subtrees is None else subtrees)
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
        recorded = {
            rel: (row["size"], row["mtime_ns"], row["sha256"])
            for rel, row in known.items()
        }
        pending: list[tuple[str, str, int, int, str | None]] = []
        restat: list[tuple[str, int, int]] = []
        removed: list[str] = []
        stale_keys: list[str] = []
        for change in diff_files(self.root, subtree, recorded, extensions):
            rel, stat = change.relpath, change.stat
            if change.kind == "deleted" or stat is None:
                removed.append(rel)
                stale_keys.append(known[rel]["fkey"])
                continue
            if change.kind == "added":
                pending.append(
                    (rel, file_key(rel), stat.st_size, stat.st_mtime_ns, None)
                )
                continue
            row = known[rel]
            if change.kind == "touched" and row["state"] != "error":
                restat.append((rel, stat.st_size, stat.st_mtime_ns))
                continue
            # Changed content, or a touched file whose last attempt failed.
            stale_keys.append(row["fkey"])
            pending.append(
                (rel, row["fkey"], stat.st_size, stat.st_mtime_ns, change.digest)
            )
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
        # LanceDBDocumentStore has no public prefix delete or table handle;
        # talkpipe issue filed for one, until then its internals are used.
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
                    digest = row["sha256"] or sha256_file(path)
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
        except Exception as exc:
            # Any failure waits before the next try, so an error that does not
            # go away (an embedder that is down, an incompatible talkpipe) is
            # reported instead of retried in a tight loop.
            if not isinstance(exc, RagIngestError):
                logger.exception("Embedding %s failed", self.root)
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
        # See _docs_table: no public drop on LanceDBDocumentStore yet.
        store = LanceDBDocumentStore(
            path=self.index_path, table_name=DEFAULT_VECTOR_TABLE_NAME
        )
        with contextlib.suppress(FileNotFoundError, ValueError):
            store._get_db().drop_table(DEFAULT_VECTOR_TABLE_NAME)

    # -- status ------------------------------------------------------------

    def _read_status(self) -> dict[str, Any]:
        return read_index_status(self.location.path)

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
            # A scanned folder with nothing to index, or whose files all
            # failed, is finished too: nothing more will happen to it.
            finished=data["phase"] == "ready" or (bool(total) and embedded == total),
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


def vectors_started(index_path: str | Path) -> bool:
    """True once a folder index has vectors for at least one file.

    A named vault (no folder status) always counts as started.
    """
    if index_folder_root(index_path) is None:
        return True
    return int(read_index_status(index_path).get("files_embedded") or 0) > 0


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
