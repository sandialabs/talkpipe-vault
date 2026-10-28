"""Folder mode: folders searched directly, indexed in the background.

The engine and its segments live in ``pipelines/folder_index.py``; this is
the glue both interfaces share (the terminal interface calls these helpers
too): opening, closing and removing folders, their indexing status and the
notices shown while they index, what a command line opens, and the chunk
and source-file lookups behind a result.
"""

import os
import threading
from pathlib import Path
from typing import Any

from talkpipe.search.whoosh import WhooshFullTextIndex

from talkpipe_vault.apps import access_control, user_settings
from talkpipe_vault.apps import state as app_state
from talkpipe_vault.apps.pipelines import _refresh_pipelines, _update_document_counts
from talkpipe_vault.apps.state import (
    AppState,
    _apply_vault_embedding_config,
    _clear_pipelines,
    _effective_models,
    _existing_non_vault_entries,
    _index_paths,
    _open_folder_indexes,
    _scheduler,
)
from talkpipe_vault.apps.vault_store import (
    _get_chunk_text_for_path_and_snippet,
    _resolve_indexed_source_file,
)
from talkpipe_vault.pipelines import folder_files, folder_index
from talkpipe_vault.pipelines.config import FOLDER_INDEX_DIRNAME, get_whoosh_index_path


def folders_status(state: AppState) -> list[dict[str, Any]]:
    """Each open folder's indexing progress and role, for both interfaces."""
    models = _effective_models(state)
    current = f"{models['embedding_source']}/{models['embedding_model']}"
    statuses = []
    for folder in _open_folder_indexes(state):
        status = folder.status()
        status["embedding_differs"] = bool(status["embedding"]) and (
            status["embedding"] != current
        )
        status["current_embedding"] = current
        status["chunking_differs"] = (
            folder.chunk_size,
            folder.shingle_size,
            folder.overlap,
        ) != (
            models["chunk_size"],
            models["shingle_size"],
            models["shingle_overlap"],
        )
        status["progress"] = _folder_progress_line(status)
        status["closed"] = False
        statuses.append(status)
    statuses.extend(
        _closed_folder_status(root) for root in user_settings.get_closed_folders()
    )
    return statuses


FOLDER_CLOSED_PROGRESS = "Closed · not searched; its index is kept"


def _closed_folder_status(root: str) -> dict[str, Any]:
    """A closed folder's entry in the folder list: no indexing, nothing to wait for."""
    return {
        "root": root,
        "name": Path(root).name or root,
        "closed": True,
        "phase": "closed",
        "progress": FOLDER_CLOSED_PROGRESS,
        "finished": True,
        "complete": True,
        "files_total": 0,
        "files_keyword": 0,
        "files_embedded": 0,
        "files_pending": 0,
        "files_error": 0,
        "files_empty": 0,
        "chunks": 0,
        "keyword_coverage": 0.0,
        "vector_coverage": 0.0,
        "in_folder": True,
        "index_path": "",
        "location_reason": "",
        "role": "closed",
        "writer": "",
        "stalled": False,
        "error": "",
        "vector_error": "",
        "watch_mode": "",
        "embedding": "",
        "current_embedding": "",
        "embedding_differs": False,
        "chunking_differs": False,
    }


def _folder_progress_line(status: dict[str, Any]) -> str:
    """One line on where a folder's indexing stands, for the folder list."""
    total = status["files_total"]
    phase = status["phase"]
    if phase == "scanning" and not total:
        return "Scanning for documents…"
    if phase in ("waiting", "stopped") and not total:
        return "Waiting for the indexer…"
    if not total:
        return "No documents found yet"
    keyword, embedded = status["files_keyword"], status["files_embedded"]
    if phase == "stalled":
        return f"Indexing stalled at {embedded}/{total} files ({status['writer']})"
    if keyword < total:
        return f"Keyword index {keyword}/{total} files"
    if embedded < total:
        paused = " (paused)" if phase == "paused" else ""
        return f"Searchable by keyword · meaning {embedded}/{total} files{paused}"
    if status.get("role") == "read-only":
        # Nobody can write this index, so changes to the files are not picked up.
        return f"Ready · {total} file{'' if total == 1 else 's'} · read-only index"
    watching = {
        "live": " · watching for changes",
        # No change events (read-only or network folder): rescans only.
        "rescan": " · checked for changes every "
        f"{folder_files.UNWATCHED_RESCAN_SECONDS // 60} minutes",
    }.get(status.get("watch_mode") or "", "")
    return f"Ready · {total} file{'' if total == 1 else 's'}{watching}"


def _folder_progress_text(status: dict[str, Any]) -> str:
    """ "notes (keywords 62%, meaning 16%)" for an unfinished folder."""
    keyword = round(100 * status["keyword_coverage"])
    meaning = round(100 * status["vector_coverage"])
    if status["phase"] == "scanning" and not status["files_total"]:
        return f"{status['name']} (scanning)"
    return f"{status['name']} (keywords {keyword}%, meaning {meaning}%)"


def _folder_banner(state: AppState) -> str:
    """The still-indexing notice every page shows, or "" when all are done."""
    unfinished = [s for s in folders_status(state) if not s["finished"]]
    if not unfinished:
        return ""
    names = ", ".join(_folder_progress_text(s) for s in unfinished)
    return (
        f"Still indexing {names}. Results may miss documents; keyword matches "
        "are included for files not embedded yet."
    )


# Notices both interfaces show, worded once; the terminal interface fills in
# its own page names (the Vault tab, the Keywords tab, ...).
SEMANTIC_SEARCH_WAITING_TEMPLATE = (
    "Semantic search starts once the first documents are embedded (progress "
    "is on {progress}). {others} work now."
)
SEMANTIC_SEARCH_WAITING = SEMANTIC_SEARCH_WAITING_TEMPLATE.format(
    progress="Vaults & Documents", others="Keyword Search and Ask"
)
KEYWORD_SEARCH_STARTING = (
    "Keyword search starts shortly: the open folders are being scanned, and "
    "their keyword index is built first."
)
FOLDERS_INDEX_THEMSELVES_TEMPLATE = (
    "Folders index themselves: add the folder under Folders above. To index "
    "into a named vault instead, {how}."
)
FOLDERS_KEEP_EMBEDDER_TEMPLATE = (
    " The embedding model changed: each folder keeps searching with the model "
    "it was indexed with until you re-embed it ({action})."
)


def semantic_search_waiting(
    state: AppState, notice: str = SEMANTIC_SEARCH_WAITING
) -> str:
    """Why semantic search is not offered yet, or "" when it is.

    In folder mode it waits until some open folder has vectors: before that
    there is nothing to rank by meaning, and embedding the query could wait
    on the model's first download. Ask answers from keywords meanwhile.
    """
    if state.folders and not any(
        folder_index.vectors_started(path) for path in _index_paths(state)
    ):
        return notice
    return ""


ASK_WAITING = (
    "Indexing has only just started, so nothing is searchable yet. Ask again "
    "in a few seconds: answers draw on keyword matches as soon as the first "
    "files are indexed (progress is on Vaults & Documents)."
)


def ask_waiting(state: AppState) -> str:
    """Ask's reply while no open folder has indexed a file yet, else "".

    Answering then would only report that the documents say nothing; an
    empty folder whose scan has finished is not waiting.
    """
    statuses = (
        [s for s in folders_status(state) if not s["closed"]] if state.folders else []
    )
    if statuses and all(
        not s["files_keyword"] and s["phase"] != "ready" for s in statuses
    ):
        return ASK_WAITING
    return ""


def _partial_index_note(state: AppState) -> str:
    """The Ask "Answered by" addition while folders are still embedding."""
    unfinished = [s for s in folders_status(state) if not s["finished"]]
    if not unfinished:
        return ""
    parts = ", ".join(
        f"{s['name']} {round(100 * s['vector_coverage'])}% embedded" for s in unfinished
    )
    return f"partial index: {parts} · keyword matches included"


def _within_path(path: str, ancestor: str) -> bool:
    return path == ancestor or path.startswith(ancestor.rstrip(os.sep) + os.sep)


def _activate_folders(state: AppState) -> None:
    """Point the pipelines at the open folders (or at nothing)."""
    paths = _index_paths(state) if state.folders else []
    if not paths:
        state.vault_path = ""
        _clear_pipelines(state)
        return
    state.vault_path = paths[0]
    # Not the first folder's recorded embedder, as for a named vault: every
    # folder is searched with its own, so the active one stays the Settings
    # choice, which new folders are indexed with and Re-embed offers.
    _apply_vault_embedding_config(None)
    _refresh_pipelines(force=True)
    _update_document_counts(state.vault_path)


# Held while the list of open folders changes: two requests adding the same
# folder at once (a double click) must not both get past the duplicate check.
_folders_lock = threading.Lock()


def add_folder(state: AppState, raw_path: str) -> tuple[str, str]:
    """Start searching the folder *raw_path* (and everything under it).

    Returns (message, error). The folder is fenced by the document roots,
    like indexing; a folder inside an open one is refused (it is already
    searched), and one containing open folders replaces them. Opening the
    first folder leaves named-vault mode. Indexing starts in the background.
    """
    with _folders_lock:
        return _add_folder(state, raw_path)


def _add_folder(state: AppState, raw_path: str) -> tuple[str, str]:
    text = raw_path.strip()
    if not text:
        return "", "Choose a folder to search."
    roots = access_control.document_roots()
    confined = access_control.confine(os.path.expanduser(text), roots)
    if confined is None:
        return "", (
            f"Folders on this server must be under {access_control.describe(roots)}."
        )
    if not confined.is_dir():
        what = "is not a folder" if confined.exists() else "does not exist"
        return "", f"{confined} {what}."
    root = str(confined)
    for existing in state.folders:
        if root == existing:
            return f"{root} is already being searched.", ""
        if _within_path(root, existing):
            return "", f"{root} is already covered by {existing}."
    models = _effective_models(state)
    try:
        folder = folder_index.open_folder(
            root,
            embedding_source=models["embedding_source"],
            embedding_model=models["embedding_model"],
            chunk_size=models["chunk_size"],
            shingle_size=models["shingle_size"],
            overlap=models["shingle_overlap"],
            index_location=user_settings.get_index_location(),
        )
    except OSError as exc:
        return "", str(exc)
    uuid = folder_index.read_index_status(folder.index_path).get("index_uuid")
    others = {
        folder_index.read_index_status(f.index_path).get("index_uuid")
        for f in _open_folder_indexes(state)
    }
    if uuid and uuid in others:
        return "", (
            f"{root} holds a copy of another open folder's index. Delete its "
            f"{FOLDER_INDEX_DIRNAME} folder to give it an index of its own."
        )
    if not state.folders and state.vault_path:
        # Leaving named-vault mode; the vault stays in the recent list.
        state.vault_path = ""
    replaced = [e for e in state.folders if _within_path(e, root)]
    for existing in replaced:
        _close_folder(state, existing)
    _scheduler().add(folder)
    state.folders.append(root)
    user_settings.set_open_folders(state.folders)
    _mark_closed(root, closed=False)
    user_settings.set_last_session(user_settings.SESSION_FOLDERS)
    _activate_folders(state)
    where = (
        "in the folder"
        if folder.location.in_folder
        else f"in app data ({folder.location.reason})"
    )
    message = (
        f"Searching {root}. Its index is kept {where}; indexing runs in the background."
    )
    if replaced:
        message += f" It replaces {', '.join(replaced)}, which it contains."
    return message, ""


def _close_folder(state: AppState, root: str) -> folder_index.FolderIndex | None:
    folder = next((f for f in _open_folder_indexes(state) if str(f.root) == root), None)
    if folder is not None and app_state._folder_scheduler is not None:
        app_state._folder_scheduler.remove(folder)
    if root in state.folders:
        state.folders.remove(root)
    return folder


def _mark_closed(root: str, *, closed: bool) -> None:
    """Add *root* to, or drop it from, the closed-folder list."""
    others = [p for p in user_settings.get_closed_folders() if p != root]
    user_settings.set_closed_folders([*others, root] if closed else others)


def close_folder(state: AppState, root: str) -> tuple[str, str]:
    """Stop searching and indexing *root* but keep it in the folder list.

    Returns (message, error). The folder's index is kept where it is, so
    opening the folder again is instant; until then it is not searched and
    its indexer does not run.
    """
    with _folders_lock:
        if root not in state.folders:
            return "", f"{root} is not being searched."
        _close_folder(state, root)
        user_settings.set_open_folders(state.folders)
        _mark_closed(root, closed=True)
        _activate_folders(state)
    message = (
        f"Closed {root}: it is not searched until you open it again. Its index "
        "is kept, so opening it is instant."
    )
    return message, ""


def open_closed_folder(state: AppState, root: str) -> tuple[str, str]:
    """Search a closed folder again (the folder-list Open action)."""
    if root not in user_settings.get_closed_folders():
        return "", f"{root} is not a closed folder."
    return add_folder(state, root)


def remove_folder(
    state: AppState, root: str, delete_index: bool = False
) -> tuple[str, str]:
    """Drop *root*, open or closed, from the folder list; with *delete_index*,
    delete its index too.

    Returns (message, error). Only the index directory is ever deleted,
    never the documents, and not while another process is indexing it.
    """
    with _folders_lock:
        if root in user_settings.get_closed_folders():
            _mark_closed(root, closed=False)
            try:
                folder: folder_index.FolderIndex | None = folder_index.open_folder(
                    root, index_location=user_settings.get_index_location()
                )
            except OSError:
                folder = None
            message = f"Removed {root} from the folder list."
        elif root not in state.folders:
            return "", f"{root} is not being searched."
        else:
            folder = _close_folder(state, root)
            user_settings.set_open_folders(state.folders)
            _activate_folders(state)
            message = f"Stopped searching {root}."
    if delete_index and folder is not None:
        problem = folder_index.delete_folder_index(folder)
        if problem:
            return message, f"Its index was not deleted: {problem}."
        message += f" Deleted its index ({folder.index_path})."
    elif folder is not None:
        message += " Its index is kept, so adding it again is instant."
    return message, ""


def _folder_by_root(state: AppState, root: str) -> folder_index.FolderIndex | None:
    return next((f for f in _open_folder_indexes(state) if str(f.root) == root), None)


def reembed_folder(state: AppState, root: str) -> tuple[str, str]:
    """Rebuild *root*'s vectors with the current Settings embedder."""
    folder = _folder_by_root(state, root)
    if folder is None:
        return "", f"{root} is not being searched."
    if not folder.is_writer:
        return "", "Another vault process is indexing this folder; re-embed it there."
    models = _effective_models(state)
    source, model = models["embedding_source"], models["embedding_model"]
    _scheduler().submit(folder, lambda: folder.reembed(source, model)).wait(30)
    _activate_folders(state)
    message = (
        f"Re-embedding {root} with {source}/{model}. Keyword search keeps "
        "answering meanwhile."
    )
    return message, ""


def rebuild_folder(state: AppState, root: str) -> tuple[str, str]:
    """Re-chunk and re-index *root* with the current chunking settings."""
    folder = _folder_by_root(state, root)
    if folder is None:
        return "", f"{root} is not being searched."
    if not folder.is_writer:
        return "", "Another vault process is indexing this folder; rebuild it there."
    models = _effective_models(state)
    _scheduler().submit(
        folder,
        lambda: folder.rebuild(
            models["chunk_size"], models["shingle_size"], models["shingle_overlap"]
        ),
    ).wait(30)
    _activate_folders(state)
    return f"Rebuilding the index of {root} with the current chunk settings.", ""


def open_saved_folders(state: AppState, roots: list[str]) -> list[str]:
    """Open several folders (startup, --resume); return the problems."""
    problems = []
    for root in roots:
        _, error = add_folder(state, root)
        if error:
            problems.append(error)
    return problems


def is_documents_folder(path: str) -> bool:
    """True when a path given on the command line should open as a folder.

    That is a folder already searched in folder mode (it holds .tpvault),
    or an existing folder of files that holds no vault data; anything else
    (a vault, an empty or missing folder) opens as a named vault, as before.
    """
    folder = Path(path).expanduser()
    if (folder / FOLDER_INDEX_DIRNAME).is_dir():
        return True
    return _existing_non_vault_entries(folder) > 0


def resumable_folders() -> list[str]:
    """The folders to reopen on --resume: the last session's, if it had any."""
    if user_settings.get_last_session() != user_settings.SESSION_FOLDERS:
        return []
    return [p for p in user_settings.get_open_folders() if Path(p).is_dir()]


def startup_folders(
    vault_path: str, dirs: list[str], *, resume: bool, as_vault: bool
) -> tuple[str, list[str], bool, str]:
    """What a command line opens: the folders, and the vault path left over.

    Shared by vault-server and vault-tui. A ``vault_path`` naming a folder
    of documents joins the ``--dir`` folders unless *as_vault*; with no
    folders, the last session's folders reopen when *resume* is set or no
    path was given. Returns (vault_path, folders, resumed, error): a vault
    path given together with ``--dir`` is an error, since folders and a
    named vault do not open together.
    """
    folders = [str(Path(p).expanduser()) for p in dirs]
    if vault_path and not as_vault and is_documents_folder(vault_path):
        folders.insert(0, str(Path(vault_path).expanduser()))
        vault_path = ""
    if vault_path and folders:
        return (
            vault_path,
            folders,
            False,
            (
                "--dir cannot be combined with a vault path: folders and a named "
                "vault do not open together."
            ),
        )
    resumed = False
    if not folders and (resume or not vault_path):
        folders = resumable_folders()
        resumed = bool(folders)
    return vault_path, folders, resumed, ""


FOLDERS_LEFT_NOTE = (
    " The folders you had open are no longer searched (folders and a named "
    "vault do not open together); add one again on the Vaults & Documents "
    "page to go back."
)
"""Appended to the message when opening a named vault closed the folders."""


def _leave_folder_mode(state: AppState) -> None:
    """Close every folder (they stay remembered) before opening a vault."""
    with _folders_lock:
        for root in list(state.folders):
            _close_folder(state, root)


def _folder_chunk(state: AppState, key: str) -> tuple[dict[str, Any], str] | None:
    """The stored chunk behind a folder result's lookup key, and its index.

    Folder chunk ids are unique across indexes, so the key names one chunk;
    Whoosh holds every chunk, embedded or not.
    """
    for path in _index_paths(state):
        whoosh_path = get_whoosh_index_path(path)
        if not os.path.isdir(whoosh_path):
            continue
        with WhooshFullTextIndex(whoosh_path) as ix:
            document = ix.get_document(key)
        if document:
            return document, path
    return None


def _folder_source_file(state: AppState, key: str) -> Path | None:
    """The document behind a folder result, confined to the open folders.

    Only a stored chunk's own path is ever served: a key that names no
    chunk resolves to nothing, so a request cannot pick an arbitrary file
    under an open folder (the indexer skips dotfiles on purpose).
    """
    found = _folder_chunk(state, key)
    if found is None:
        return None
    candidate = str(found[0].get("path", ""))
    root = folder_index.index_folder_root(found[1])
    recorded = folder_index.read_index_status(found[1]).get("recorded_root")
    if root is not None and recorded:
        candidate = folder_index.rebase_source(candidate, str(recorded), str(root))
    confined = access_control.confine(candidate, [Path(r) for r in state.folders])
    return confined if confined is not None and confined.is_file() else None


def chunk_text(state: AppState, key: str, snippet: str = "") -> str:
    """The full text of the chunk a result's lookup key names ("" if none).

    Both interfaces resolve results through this: in folder mode the key is
    a chunk id found in the folders' keyword indexes, else it is a docs-row
    id or indexed path in the open vault.
    """
    if state.folders:
        found = _folder_chunk(state, key)
        return str(found[0].get("content", "")) if found else ""
    return next(
        (
            text
            for index in _index_paths(state)
            if (text := _get_chunk_text_for_path_and_snippet(index, key, snippet))
        ),
        "",
    )


def resolve_source_file(state: AppState, key: str) -> Path | None:
    """The source document behind a result's lookup key, or None."""
    if state.folders:
        return _folder_source_file(state, key)
    return _resolve_indexed_source_file(state.vault_path, key)
