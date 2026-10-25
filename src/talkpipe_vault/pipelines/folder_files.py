"""The files of a folder, for folder mode's components.

What is a document file, where a folder's per-folder state may live, how
changes to the folder are noticed, and how the folder is compared with a
record of it. The indexer (``folder_index``) and the ``folderChanges`` source
(``folder_changes``) both build on this module and share nothing else, so a
third component can apply the same rules without touching either.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from talkpipe.data.extraction import global_extractor_registry
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.utils.patterns import match_any_paths

from talkpipe_vault.watchdog import COMMON_IGNORE_PATTERNS

from .config import FOLDER_INDEX_DIRNAME

logger = logging.getLogger(__name__)

ROOT_RECORD_FILENAME = "root.json"
EXTERNAL_INDEX_DIRNAME = "folder-indexes"

# A full rescan catches changes made while no instance ran (at open) and
# events the watcher missed; folders without live events rescan more often.
RESCAN_SECONDS = 30 * 60
UNWATCHED_RESCAN_SECONDS = 10 * 60
# Watcher events are collected for this long before the changed files are
# compared, so a burst of saves is handled once.
WATCH_DEBOUNCE_SECONDS = 2.0

# Filesystem types whose files are reached over the network. Per-folder state
# for a folder on one of these is kept on local disk instead of in the folder,
# so no correctness argument ever depends on file locks working across hosts.
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
# Where a folder's state lives


@dataclass(frozen=True)
class IndexLocation:
    """Where a folder's index (or other per-folder state) is kept.

    ``reason`` says why it is outside the folder (empty when it is inside),
    and ``read_only`` is set for an existing index that cannot be written,
    which opens as a reader only.
    """

    path: Path
    in_folder: bool
    read_only: bool = False
    reason: str = ""


def external_index_path(
    root: Path, base: Path, dirname: str = EXTERNAL_INDEX_DIRNAME
) -> Path:
    """The app-data location of a folder's index, unique per folder path."""
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    name = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in root.name)
    return base / dirname / f"{name.strip('-._') or 'folder'}-{digest}"


def mount_type(path: Path) -> str:
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


def sync_client(root: Path) -> str:
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
    if mount_type(root) in NETWORK_FS_TYPES:
        return "the folder is on a network share"
    client = sync_client(root)
    if client:
        return f"the folder is synced ({client})"
    return ""


def resolve_index_location(
    root: str | Path,
    external_base: Path,
    *,
    force_external: str = "",
    dirname: str = FOLDER_INDEX_DIRNAME,
    external_dirname: str = EXTERNAL_INDEX_DIRNAME,
) -> IndexLocation:
    """Decide where the index of the folder *root* lives.

    First match wins: an index already in the folder; an index already in
    app data (``external_base``); app data when *force_external* gives a
    reason (a fenced deployment, or the user's choice) or the folder is on a
    network share or synced; otherwise the folder itself, if a ``.tpvault``
    can be created and written there. *dirname* and *external_dirname* name
    the two places, so other per-folder state (folderChanges') follows
    the same rules.
    """
    root = Path(os.path.realpath(root))
    in_tree = root / dirname
    if in_tree.is_dir():
        writable = os.access(in_tree, os.W_OK)
        return IndexLocation(
            in_tree,
            True,
            read_only=not writable,
            reason="" if writable else "the folder's index is read-only",
        )
    external = external_index_path(root, external_base, external_dirname)
    if external.is_dir():
        return IndexLocation(external, False, reason=recorded_reason(external))
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


def recorded_reason(external: Path) -> str:
    """Why an index in app data was put there, as recorded when it was made."""
    try:
        data = json.loads((external / ROOT_RECORD_FILENAME).read_text("utf-8"))
        return str(data.get("reason") or "kept in app data")
    except (OSError, ValueError, AttributeError):
        return "kept in app data"


def prepare_index_dir(
    location: IndexLocation, root: Path, cache_tag: bool = True
) -> None:
    """Create the state directory with its hygiene files.

    ``.gitignore`` makes git skip the directory without editing the user's
    own ignore file, and ``CACHEDIR.TAG`` makes borg, restic
    ``--exclude-caches`` and ``tar --exclude-caches`` skip it. A directory in
    app data records which folder it belongs to.
    """
    location.path.mkdir(parents=True, exist_ok=True)
    gitignore = location.path / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    tag = location.path / "CACHEDIR.TAG"
    if cache_tag and not tag.exists():
        tag.write_text(CACHEDIR_TAG, encoding="utf-8")
    if not location.in_folder:
        record = location.path / ROOT_RECORD_FILENAME
        record.write_text(
            json.dumps({"root": str(root), "reason": location.reason}, indent=2),
            encoding="utf-8",
        )


# --------------------------------------------------------------------------
# Which files count, and what changed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def is_hidden(relpath: str) -> bool:
    return any(part.startswith(".") for part in relpath.split("/"))


def supported_extensions() -> set[str]:
    """Extensions a talkpipe extractor reads; plugins may add some at any time."""
    return {ext.lower() for ext in global_extractor_registry.registered_extensions}


def within(path: str, ancestor: str) -> bool:
    """Whether the relative *path* is *ancestor* or below it ("" covers all)."""
    return ancestor == "" or path == ancestor or path.startswith(ancestor + "/")


def covering_subtrees(subtrees: Iterable[str]) -> list[str]:
    """*subtrees* without those that lie inside another of them."""
    roots = list(subtrees)
    return [r for r in roots if not any(o != r and within(r, o) for o in roots)]


def changed_relpath(root: Path, path: str) -> str:
    """*path* relative to *root* when a change to it matters, else "".

    watchdog's pattern matching looks only at a path's last component, so a
    ``.*`` ignore does not stop events from inside ``.tpvault`` or ``.git``;
    any path with a hidden component is dropped here instead, or an index's
    own writes would keep re-triggering its indexer.
    """
    try:
        rel = Path(path).relative_to(root).as_posix()
    except ValueError:
        return ""
    if rel in ("", ".") or is_hidden(rel):
        return ""
    if not match_any_paths(
        [path], excluded_patterns=COMMON_IGNORE_PATTERNS, case_sensitive=False
    ):
        return ""
    return rel


def document_files(
    root: Path, subtree: str, extensions: set[str]
) -> dict[str, os.stat_result]:
    """Supported, visible document files at or below *subtree* of *root*.

    Hidden paths, temporary files and symlinks leading out of *root* are
    left out; *subtree* "" means the whole folder.
    """
    found: dict[str, os.stat_result] = {}
    start = root / subtree if subtree else root

    def consider(path: Path) -> None:
        rel = path.relative_to(root).as_posix()
        if is_hidden(rel) or path.suffix[1:].lower() not in extensions:
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
                if not target.startswith(str(root) + os.sep):
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


@dataclass(frozen=True)
class FileChange:
    """One difference between a folder and a record of it.

    ``kind`` is "added", "changed" (new content), "touched" (the stat changed
    but the content did not) or "deleted"; ``stat`` is None for a deleted
    file and ``digest`` is the content hash when one was computed.
    """

    relpath: str
    kind: str
    stat: os.stat_result | None
    digest: str | None


def diff_files(
    root: Path,
    subtree: str,
    known: Mapping[str, tuple[int, int, str | None]],
    extensions: set[str],
    *,
    hash_new: bool = False,
) -> Iterator[FileChange]:
    """Compare the files at or below *subtree* with *known*, a record of them.

    *known* maps each recorded relative path to its (size, mtime_ns, sha256).
    Deletions come first, so a rename reads as the old path going and the
    new one arriving; the rest follow in path order. A file whose size or
    modification time changed is hashed, so a touched file with the same
    content is reported as "touched" rather than "changed". New files are
    hashed only with *hash_new*. A file that cannot be read while hashing is
    left out: a later comparison decides about it.
    """
    on_disk = document_files(root, subtree, extensions)
    for rel in sorted(set(known) - set(on_disk)):
        yield FileChange(rel, "deleted", None, None)
    for rel in sorted(on_disk):
        stat = on_disk[rel]
        row = known.get(rel)
        if row is None:
            digest = None
            if hash_new:
                try:
                    digest = sha256_file(root / rel)
                except OSError:
                    continue
            yield FileChange(rel, "added", stat, digest)
            continue
        if row[0] == stat.st_size and row[1] == stat.st_mtime_ns:
            continue
        try:
            digest = sha256_file(root / rel)
        except OSError:
            continue
        kind = "touched" if digest == row[2] else "changed"
        yield FileChange(rel, kind, stat, digest)


# --------------------------------------------------------------------------
# Noticing changes


class _ChangeHandler(FileSystemEventHandler):
    """Passes every path watchdog reports as changed to *note_change*."""

    def __init__(self, note_change: Callable[[str], None]):
        self.note_change = note_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            if raw:
                self.note_change(os.fsdecode(raw))


def watch(root: Path, note_change: Callable[[str], None]) -> tuple[Any, str]:
    """Start watching *root*; return the observer (or None) and the mode.

    The mode is "live" with change events, or "rescan" where they are not
    reliable (a network share, a folder this process cannot write) or
    watching failed, so the caller relies on periodic rescans.
    """
    if mount_type(root) in NETWORK_FS_TYPES or not os.access(root, os.W_OK):
        return None, "rescan"
    try:
        observer = Observer()
        observer.schedule(_ChangeHandler(note_change), str(root), recursive=True)
        observer.daemon = True
        observer.start()
    except Exception as exc:
        # inotify watch limits, unsupported filesystems, and the like.
        logger.warning("Watching %s failed (%s); rescanning instead", root, exc)
        return None, "rescan"
    return observer, "live"


def stop_observer(observer: Any) -> None:
    if observer is not None:
        with contextlib.suppress(Exception):
            observer.stop()
            observer.join(timeout=5)


class ChangeTracker:
    """Decides when a folder needs comparing with a record of it, and where.

    Watcher events are collected (``note_change``) and debounced; a full
    comparison is due when the tracker starts or is ``reset`` and then
    periodically, more often where events are unreliable. ``due`` returns
    the subtrees to compare now: ``[""]`` for the whole folder, the paths
    the watcher reported, or None.
    """

    def __init__(self, root: Path):
        self.root = root
        self.watch_mode = ""
        self._observer: Any = None
        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self._last_change = 0.0
        self._last_scan = 0.0

    def start_watching(self) -> None:
        self._observer, self.watch_mode = watch(self.root, self.note_change)

    def stop_watching(self) -> None:
        observer, self._observer = self._observer, None
        stop_observer(observer)

    def reset(self) -> None:
        """Make the next ``due`` a full comparison."""
        self._last_scan = 0.0

    @property
    def dirty(self) -> set[str]:
        """The paths reported changed and not yet compared."""
        with self._lock:
            return set(self._dirty)

    def note_change(self, path: str) -> None:
        """Record that *path* changed; it is compared after a short delay."""
        rel = changed_relpath(self.root, path)
        if rel:
            with self._lock:
                self._dirty.add(rel)
                self._last_change = time.monotonic()

    def due(self, now: float) -> list[str] | None:
        interval = (
            RESCAN_SECONDS if self.watch_mode == "live" else UNWATCHED_RESCAN_SECONDS
        )
        with self._lock:
            if not self._last_scan or now - self._last_scan >= interval:
                self._dirty.clear()
                self._last_scan = now
                return [""]
            if self._dirty and now - self._last_change >= WATCH_DEBOUNCE_SECONDS:
                dirty, self._dirty = self._dirty, set()
                return covering_subtrees(sorted(dirty))
        return None
