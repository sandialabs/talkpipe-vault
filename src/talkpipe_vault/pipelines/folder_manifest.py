"""The per-file state of a folder index, in sqlite (``manifest.db``)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "manifest.db"
MANIFEST_SCHEMA_VERSION = 1


class Manifest:
    """The writer's per-file indexing state.

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
