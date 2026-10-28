"""Which indexes a search component searches, and how it keeps that current.

A component takes one path or several, each a named vault or a folder of
documents; ``resolve_vault_paths`` maps a folder to its index and
``check_searchable`` refuses what cannot be searched. ``_SearchesVaultPaths``
is the mixin the vault's search segments share: build what depends on the
paths, and rebuild it when the indexes behind them change. ``_IndexView``
labels each index's hits with its folder and rebases their sources when the
folder moved.
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import folder_index
from .config import ensure_supported_vault_layout, existing_non_vault_entries


def resolve_vault_paths(vault_path: str | Iterable[str]) -> list[str]:
    """The index paths a segment's vault_path names.

    One path or several. A folder that has a folder-mode index (its own
    ``.tpvault``, or one kept in app data) stands for that index, so a
    segment can be pointed at the documents folder itself; any other path
    is a vault and is used as it is.
    """
    paths = [vault_path] if isinstance(vault_path, str) else list(vault_path)
    resolved: list[str] = []
    for raw in paths:
        path = os.path.expanduser(str(raw))
        index = None
        if os.path.isdir(path) and folder_index.index_folder_root(path) is None:
            index = folder_index.find_index_path(path)
        resolved.append(str(index) if index is not None else path)
    if not resolved:
        raise ValueError("vault_path names no vault")
    return resolved


def check_searchable(paths: list[str]) -> None:
    """Refuse a search of a path that does not exist, or of a folder of
    documents that has no index yet, with a ValueError that says so.

    Searching either would otherwise create folders (an empty vault at a
    mistyped path, a full-text index inside the documents folder) and
    quietly find nothing. Checked when a search runs, not when the segment
    is built, so an interface can build its pipelines for a vault before
    the vault is indexed or while it reports one that went missing.
    """
    for path in paths:
        if not os.path.isdir(path):
            what = "is not a folder" if os.path.exists(path) else "does not exist"
            raise ValueError(f"Cannot search {path!r}: it {what}. Check the path.")
        if (
            folder_index.index_folder_root(path) is None
            and existing_non_vault_entries(Path(path)) > 0
        ):
            raise ValueError(
                f"Cannot search {path!r}: it holds documents but no index yet. "
                "Index a folder with indexFolder (once) or watchFolders (kept "
                "up to date), or open it in vault-server or vault-tui; build a "
                "named vault with makevectordatabase or the Vaults & Documents "
                "page."
            )


class _SearchesVaultPaths:
    """Shared by the segments that search one or more vaults or folders.

    A folder stands for its index once it has one, and a segment's search
    is built on the indexes found when the segment is made. One made before
    a folder was indexed (while watchFolders, say, is still starting) would
    otherwise refuse that folder on every query even after it is indexed, so
    before each query the paths are resolved again and, when the indexes
    behind ``vault_path`` changed, ``_build`` is run again for the new ones.
    """

    vault_path: str | list[str]
    _paths: list[str]

    def _build(self, paths: list[str]) -> None:
        """Build the search for *paths*; set only what depends on them."""
        raise NotImplementedError

    def _resolve_and_build(self) -> None:
        paths = resolve_vault_paths(self.vault_path)
        for path in paths:
            ensure_supported_vault_layout(path)
        self._paths = paths
        self._build(paths)

    def _ensure_searchable(self) -> None:
        """Rebuild for indexes that appeared or moved, then refuse bad paths."""
        if resolve_vault_paths(self.vault_path) != self._paths:
            self._resolve_and_build()
        check_searchable(self._paths)


def needs_fan_out(paths: list[str]) -> bool:
    """True unless *paths* is one named vault, which keeps its own pipeline.

    Several indexes are searched one by one and fused; a folder index goes
    the same way even alone, so its results carry the folder and have their
    paths rebased when the folder moved.
    """
    return len(paths) > 1 or folder_index.index_folder_root(paths[0]) is not None


@dataclass(frozen=True)
class _IndexView:
    """One index in a fan-out search: where it is and how to label its hits.

    ``recorded_root`` is the folder path its chunk sources were stored under
    and ``current_root`` where the folder is now; they differ when the
    folder was moved or is mounted elsewhere (host versus container), and
    sources are rebased from one to the other.
    """

    path: str
    label: str
    current_root: str = ""
    recorded_root: str = ""

    def tag(self, document: Any) -> dict[str, Any]:
        doc = dict(document or {})
        doc["folder"] = self.label
        for key in ("source", "path"):
            value = doc.get(key)
            if isinstance(value, str):
                doc[key] = folder_index.rebase_source(
                    value, self.recorded_root, self.current_root
                )
        return doc


def _index_views(paths: list[str]) -> list[_IndexView]:
    """Views for the indexes, labelled by folder (or vault) name.

    Two folders with the same name are told apart by their parent's name.
    """
    entries: list[tuple[str, Path, str, str]] = []
    for path in paths:
        root = folder_index.index_folder_root(path)
        if root is None:
            entries.append((path, Path(path), "", ""))
            continue
        recorded = folder_index.read_index_status(path).get("recorded_root")
        entries.append((path, root, str(root), str(recorded or root)))
    names = [where.name or str(where) for _, where, _, _ in entries]
    views = []
    for (path, where, current, recorded), name in zip(entries, names, strict=True):
        label = name
        if names.count(name) > 1 and where.parent.name:
            label = f"{name} ({where.parent.name})"
        views.append(_IndexView(path, label, current, recorded))
    return views
