"""Named vaults: opening, suggesting, deleting, and the background indexing jobs.

Validating and activating a vault path, the retrieval-filter editor's
context, the documents-folder suggestion, indexing documents into a vault
through TalkPipe's ``build_rag_database`` driver, and the two background
jobs (document indexing, full-text index rebuild) whose progress the pages
poll.
"""

import contextlib
import glob as globlib
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import filelock
from talkpipe.pipelines.vector_databases import (
    EmbedderPreflightError,
    EmbeddingDimensionMismatchError,
    RagIngestError,
    build_rag_database,
)

from talkpipe_vault.apps import access_control, user_settings
from talkpipe_vault.apps import state as app_state
from talkpipe_vault.apps.folders import _leave_folder_mode
from talkpipe_vault.apps.pipelines import (
    _refresh_pipelines,
    _update_document_counts,
    init_pipelines,
)
from talkpipe_vault.apps.state import AppState, _existing_non_vault_entries, _state
from talkpipe_vault.apps.vault_store import (
    _build_whoosh_index,
    _indexed_source_paths,
    _iter_lancedb_docs_for_whoosh,
)
from talkpipe_vault.pipelines import folder_index, retrieval_filter, vault_metadata
from talkpipe_vault.pipelines.config import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SHINGLE_OVERLAP,
    DEFAULT_SHINGLE_SIZE,
    DEFAULT_VECTOR_TABLE_NAME,
    ensure_supported_vault_layout,
    get_retrieval_template,
    get_vector_db_path,
)


def _resolve_vault_request(raw_vault_path: str) -> tuple[Path | None, str, str]:
    """Validate a user-supplied vault path.

    Returns (vault_path, placement_note, error): the path is None when the
    error message is non-empty. The note explains any automatic relocation and
    belongs in the success message shown to the user.
    """
    raw_path = raw_vault_path.strip()
    if not raw_path:
        return None, "", "Enter a vault path."

    # Pure string expansion; the filesystem is only ever touched through the
    # confined path below.
    expanded = os.path.expanduser(raw_path)

    # Failsafe for fenced deployments (e.g. the container, where vaults live
    # under /app/data): a bare name like "my-vault" would otherwise resolve
    # against the server's working directory and be refused by the fence.
    # Place it under the vault root instead and tell the user below.
    root = access_control.vault_root()
    placed_under_root = False
    if root is not None and not os.path.isabs(expanded):
        expanded = os.path.join(os.fspath(root), expanded)
        placed_under_root = True
    placement_note = (
        f" (You entered '{raw_path}'; vaults on this server live under "
        f"{root}, so it was placed there automatically.)"
        if placed_under_root
        else ""
    )

    # Every later filesystem touch (existence checks, mkdir, indexing) uses
    # the confined (resolved and root-checked) path, never the raw request.
    confined = access_control.confine_vault(expanded)
    if confined is None:
        return (
            None,
            "",
            f"Vaults on this server must live under {access_control.vault_root()}.",
        )
    if confined.is_file():
        return None, "", f"{confined} is a file. A vault must be a directory."
    return confined, placement_note, ""


def _activate_vault(vault_path: Path) -> str:
    """Create the vault folder if needed, open it, and remember it.

    Returns an error message, or an empty string when the vault is open.
    """
    try:
        vault_path.mkdir(parents=True, exist_ok=True)
        _leave_folder_mode(_state)
        init_pipelines(str(vault_path))
    except (OSError, ValueError) as exc:
        return str(exc)
    user_settings.remember_vault(str(vault_path))
    user_settings.set_last_session(user_settings.SESSION_VAULT)
    return ""


def _is_dangerous_delete_target(real_path: Path) -> bool:
    """Return True for paths too broad to ever delete as a vault.

    Guards against wiping the filesystem root, the user's home directory, or
    other shallow top-level paths even if they somehow reach this code.
    """
    home = Path(os.path.realpath(Path.home()))
    return (
        real_path == real_path.parent  # filesystem root
        or real_path == home
        or len(real_path.parts) < 3
    )


def _vault_source_folder(state: AppState) -> str:
    """The folder every document of the open named vault lies in, or "".

    A named vault cannot be converted in place (its chunk ids are random),
    but when its documents share one folder the page offers to search that
    folder directly instead. Computed once per opened vault: it reads the
    docs table's paths.
    """
    if state.folders or not state.vault_path:
        return ""
    if state.vault_source_folder is None:
        state.vault_source_folder = ""
        with contextlib.suppress(Exception):
            sources = [
                path
                for path in _indexed_source_paths(state.vault_path)
                if os.path.isabs(path)
            ]
            if sources:
                common = Path(os.path.commonpath(sources))
                if common.is_file():
                    common = common.parent
                real = Path(os.path.realpath(common))
                if common.is_dir() and not _is_dangerous_delete_target(real):
                    state.vault_source_folder = str(common)
    return state.vault_source_folder


def retrieval_filter_blocker(state: AppState) -> str:
    """Why the retrieval filter cannot be edited now, or "" when it can.

    A filter belongs to one index: the open named vault or the one open
    folder. Both interfaces show the editor, and accept a save, only then.
    """
    if not state.vault_path:
        return "Open a folder or a vault before configuring its retrieval filter."
    if len(state.folders) > 1:
        return (
            "A retrieval filter applies to one open folder; close the others "
            "to edit it."
        )
    return ""


def _retrieval_filter_context(state: AppState, **pending: Any) -> dict[str, Any]:
    """Build the retrieval-filter section context for the documents page.

    ``pending`` overrides let the save/validate route re-render the page with
    the submitted (possibly unsaved) values and outcome instead of what is on
    disk, keeping the user's text in the editor.
    """
    if retrieval_filter_blocker(state):
        return {"available": False}
    script = retrieval_filter.load_script(state.vault_path) or ""
    flags = user_settings.get_retrieval_filter_flags(state.vault_path)
    error = retrieval_filter.validate_script(script) if script else None
    context: dict[str, Any] = {
        "available": True,
        # What the filter belongs to, as the page should name it: the open
        # folder (its index holds the script) or the named vault.
        "kind": "folder" if state.folders else "vault",
        "target": state.folders[0] if state.folders else state.vault_path,
        "script": script,
        "enabled": flags["enabled"],
        "strict": flags["strict"],
        "error": error,
        "active": bool(script) and flags["enabled"] and not error,
        "open": False,
        "message": "",
    }
    context.update(pending)
    return context


def suggest_vault_path(source_path: str) -> str:
    """Suggest a vault folder to create for a chosen documents folder.

    The name follows the documents folder ("~/notes" -> "<root>/notes-vault")
    so the vault is recognizable, and lands beside it under the configured
    vault root (the home directory when unrestricted). An existing folder is
    only suggested when it is empty or already holds vault data — otherwise the
    name is numbered until it is free, so the suggestion never proposes writing
    index files into a folder of someone's documents. Returns "" when no
    sensible name can be derived.
    """
    prefix = _nonglob_prefix(os.path.expanduser(source_path.strip()))
    if not prefix or prefix == ".":
        return ""
    # The chosen documents folder is fenced by the document roots (the same
    # fence indexing itself applies), not the picker's browse union.
    base = access_control.confine(prefix, access_control.document_roots())
    if base is None:
        return ""
    if base.is_file():
        base = base.parent
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", base.name).strip("-._")
    if not stem:
        return ""

    parent = access_control.vault_root() or Path.home()
    for attempt in range(1, 51):
        name = f"{stem}-vault" if attempt == 1 else f"{stem}-vault-{attempt}"
        candidate = access_control.confine_vault(parent / name)
        if candidate is None:
            return ""
        if not candidate.exists():
            return str(candidate)
        # Reusable: an empty folder, or one that already holds this vault.
        if candidate.is_dir() and _existing_non_vault_entries(candidate) == 0:
            return str(candidate)
    return ""


def _resolve_source_pattern(raw_source: str) -> str:
    """Turn a folder path into a recursive glob; leave explicit globs alone.

    The directory check runs on the confined (resolved and root-checked)
    path so nothing outside the document roots is ever touched; a source
    outside the fence keeps its raw form and is rejected by the indexing
    endpoint's own containment check.
    """
    source = os.path.expanduser(raw_source.strip())
    confined = access_control.confine(source, access_control.document_roots())
    if confined is not None and confined.is_dir():
        return os.path.join(source, "**", "*")
    return source


def _nonglob_prefix(pattern: str) -> str:
    """Return the leading part of a glob pattern up to the first wildcard.

    This is the concrete directory a pattern walks from, which is what the
    document-root restriction is checked against.
    """
    prefix_parts: list[str] = []
    for part in Path(pattern).parts:
        if any(char in part for char in "*?["):
            break
        prefix_parts.append(part)
    if not prefix_parts:
        return "."
    return str(Path(*prefix_parts))


def index_documents_into_vault(
    vault_path: str,
    source_pattern: str,
    embedding_model: str,
    embedding_source: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    shingle_size: int = DEFAULT_SHINGLE_SIZE,
    shingle_overlap: int = DEFAULT_SHINGLE_OVERLAP,
    overwrite: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[int, int]:
    """Index documents matching a glob into the vault's docs table.

    Thin wrapper over TalkPipe's build_rag_database (the same driver behind
    the makevectordatabase CLI) so the resulting table matches what the
    search/chat pipelines expect: the driver preflights the embedder,
    truncates over-long chunks instead of aborting, and counts chunks whose
    embedding failed. This wrapper adds the vault specifics — the recorded
    per-vault embedding dimension is passed as the expected dimension, driver
    errors are reworded against the web UI (Settings page, Overwrite
    checkbox), and the embedding config is recorded to vault_metadata.json.
    Returns (indexed_chunks, skipped_chunks). The optional progress callback
    receives (chunks_done, files_done, current_source_path) as chunks flow
    through the pipeline.
    """
    ensure_supported_vault_layout(vault_path)
    recorded = None if overwrite else vault_metadata.load_embedding_config(vault_path)
    try:
        result = build_rag_database(
            source_pattern,
            path=get_vector_db_path(vault_path),
            embedding_model=embedding_model,
            embedding_source=embedding_source,
            table_name=DEFAULT_VECTOR_TABLE_NAME,
            chunk_size=chunk_size,
            shingle_size=shingle_size,
            overlap=shingle_overlap,
            overwrite=overwrite,
            batch_size=25,
            expected_dimension=(recorded or {}).get("dimension"),
            progress=progress,
        )
    except EmbeddingDimensionMismatchError as exc:
        recorded = recorded or {}
        raise RuntimeError(
            f"Nothing was indexed: this vault was indexed with "
            f"{recorded.get('source')}/{recorded.get('model')} "
            f"({exc.expected}-dimensional vectors), but "
            f"{embedding_source}/{embedding_model} produces "
            f"{exc.actual}-dimensional vectors, so adding to the existing "
            f"index would fail. Check Overwrite to rebuild the vault with the "
            f"new embedder, or restore the original embedding settings."
        ) from exc
    except EmbedderPreflightError as exc:
        raise RuntimeError(
            f"Nothing was indexed: {exc} Embedding settings can be changed "
            f"under Settings."
        ) from exc
    except RagIngestError as exc:
        raise RuntimeError(f"Nothing was indexed: {exc}") from exc
    if result.chunks_indexed > 0:
        _record_vault_embedding_config(
            vault_path, embedding_source, embedding_model, result.dimension
        )
    return result.chunks_indexed, result.chunks_skipped


def _record_vault_embedding_config(
    vault_path: str,
    embedding_source: str,
    embedding_model: str,
    dimension: int | None = None,
) -> None:
    """Record how this vault was indexed so reopening restores the embedder.

    Best effort: metadata problems must never fail an indexing run that already
    wrote its vectors.
    """
    try:
        retrieval_template = get_retrieval_template(embedding_model)
    except Exception:  # pragma: no cover - defensive; config read is cheap
        retrieval_template = None
    vault_metadata.record_embedding_config(
        vault_path,
        source=embedding_source,
        model=embedding_model,
        dimension=(
            dimension
            if dimension is not None
            else vault_metadata.probe_embedding_dimension(
                embedding_source, embedding_model
            )
        ),
        retrieval_template=retrieval_template,
        server_url=_indexing_server_url(embedding_source),
    )


def _indexing_server_url(embedding_source: str) -> str | None:
    """Non-authoritative breadcrumb for where the embedder ran.

    Only meaningful for a server-backed embedder whose location the user set
    explicitly (Ollama). It is recorded for traceability and never applied on
    open — the live URL is always resolved fresh from the environment.
    """
    if embedding_source == "ollama":
        return os.environ.get("TALKPIPE_OLLAMA_SERVER_URL") or None
    return None


@dataclass
class IndexJob:
    """Progress of the (single) background document-indexing job."""

    running: bool = False
    phase: str = ""  # "counting" while the source walk runs, then "indexing"
    source: str = ""
    vault_path: str = ""
    embedding: str = ""
    total_files: int = 0
    files_done: int = 0
    current_file: str = ""
    chunks: int = 0
    message: str = ""
    error: str | None = None


_index_job = IndexJob()
_index_job_lock = threading.Lock()


def _index_job_snapshot() -> dict[str, Any]:
    with _index_job_lock:
        return asdict(_index_job)


@contextlib.contextmanager
def _index_run_guard(vault_path: str) -> Iterator[None]:
    """Held while a named-vault run writes the vault.

    No other vault process may index the same vault meanwhile (two
    concurrent runs would interleave their writes), and this process's
    folder indexer waits, so only one embedding run is in memory at a time.
    """
    lock = filelock.FileLock(
        os.path.join(vault_path, folder_index.WRITER_LOCK_FILENAME),
        is_singleton=False,
    )
    try:
        lock.acquire(timeout=0)
    except filelock.Timeout:
        raise RuntimeError(
            "Another vault process is indexing this vault; wait for it to finish."
        ) from None
    try:
        with (
            app_state._folder_scheduler.paused()
            if app_state._folder_scheduler is not None
            else contextlib.nullcontext()
        ):
            yield
    finally:
        lock.release()


def _run_index_job(
    vault_path: str,
    source: str,
    pattern: str,
    embedding_model: str,
    embedding_source: str,
    chunk_size: int,
    shingle_size: int,
    shingle_overlap: int,
    overwrite: bool,
) -> None:
    """Thread target: run one indexing job, publishing progress as it goes.

    Starts with a counting pass over the source tree (published as phase
    "counting" with a live file count) so large trees show activity instead
    of appearing hung, then switches to phase "indexing" for the embed run.
    """

    def report(chunks: int, files_done: int, current: str) -> None:
        with _index_job_lock:
            _index_job.chunks = chunks
            _index_job.files_done = files_done
            _index_job.current_file = Path(current).name if current else ""

    try:
        with _index_run_guard(vault_path):
            any_match = False
            total_files = 0
            for matched in globlib.iglob(pattern, recursive=True):
                any_match = True
                if os.path.isfile(matched):
                    total_files += 1
                    with _index_job_lock:
                        _index_job.total_files = total_files
            if not any_match:
                with _index_job_lock:
                    _index_job.error = (
                        f"'{source}' matched no files. Check the folder path "
                        "or glob pattern."
                    )
                return
            with _index_job_lock:
                _index_job.phase = "indexing"

            chunk_count, skipped = index_documents_into_vault(
                vault_path=vault_path,
                source_pattern=pattern,
                embedding_model=embedding_model,
                embedding_source=embedding_source,
                chunk_size=chunk_size,
                shingle_size=shingle_size,
                shingle_overlap=shingle_overlap,
                overwrite=overwrite,
                progress=report,
            )
            _refresh_pipelines(force=True)
            _update_document_counts(vault_path)
            with _index_job_lock:
                if chunk_count == 0:
                    _index_job.error = (
                        "The matched files contained no readable document "
                        "content; nothing was indexed."
                    )
                else:
                    _index_job.message = (
                        f"Indexed {chunk_count} chunk(s) from "
                        f"{_index_job.files_done} file(s) using "
                        f"{embedding_source}/{embedding_model}."
                    )
                    if skipped:
                        _index_job.message += (
                            f" Skipped {skipped} chunk(s) that failed to embed "
                            "— check the server log for details."
                        )
    except Exception as exc:
        with _index_job_lock:
            _index_job.error = str(exc)
    finally:
        with _index_job_lock:
            _index_job.running = False
            _index_job.current_file = ""


def start_index_job(
    vault_path: str,
    source: str,
    pattern: str,
    embedding_model: str,
    embedding_source: str,
    chunk_size: int,
    shingle_size: int,
    shingle_overlap: int,
    overwrite: bool,
) -> bool:
    """Start the background indexing job; False if one is already running."""
    global _index_job
    with _index_job_lock:
        if _index_job.running:
            return False
        # A fresh instance resets every other field to its dataclass default.
        _index_job = IndexJob(
            running=True,
            phase="counting",
            source=source,
            vault_path=vault_path,
            embedding=f"{embedding_source}/{embedding_model}",
        )

    thread = threading.Thread(
        target=_run_index_job,
        args=(
            vault_path,
            source,
            pattern,
            embedding_model,
            embedding_source,
            chunk_size,
            shingle_size,
            shingle_overlap,
            overwrite,
        ),
        daemon=True,
    )
    thread.start()
    return True


@dataclass
class FulltextIndexJob:
    """Progress of the (single) background full-text (Whoosh) index build."""

    running: bool = False
    vault_path: str = ""
    total_docs: int = 0
    docs_done: int = 0
    message: str = ""
    error: str | None = None


_fulltext_index_job = FulltextIndexJob()
_fulltext_index_job_lock = threading.Lock()


def _fulltext_index_job_snapshot() -> dict[str, Any]:
    with _fulltext_index_job_lock:
        return asdict(_fulltext_index_job)


def _run_fulltext_index_job(vault_path: str) -> None:
    """Thread target: rebuild the Whoosh index, publishing progress as it goes."""
    started = time.monotonic()
    next_log_pct = 0

    def report(docs_done: int, total_docs: int) -> None:
        nonlocal next_log_pct
        with _fulltext_index_job_lock:
            _fulltext_index_job.docs_done = docs_done
            _fulltext_index_job.total_docs = total_docs
        # Console progress: log on each ~10% step so long builds stay legible.
        pct = int(docs_done / total_docs * 100) if total_docs else 0
        if pct >= next_log_pct:
            print(
                f"  full-text index: {docs_done}/{total_docs} docs ({pct}%)",
                flush=True,
            )
            next_log_pct = (pct // 10 + 1) * 10

    try:
        with _index_run_guard(vault_path):
            documents = _iter_lancedb_docs_for_whoosh(vault_path)
            with _fulltext_index_job_lock:
                _fulltext_index_job.total_docs = len(documents)
            print(
                f"Building full-text index: {len(documents)} document(s) from {vault_path}",
                flush=True,
            )
            _build_whoosh_index(vault_path, documents, progress=report)
            _refresh_pipelines(force=True)
            _update_document_counts(vault_path)
            elapsed = time.monotonic() - started
            print(
                f"Full-text index built: {len(documents)} document(s) in {elapsed:.1f}s",
                flush=True,
            )
            with _fulltext_index_job_lock:
                _fulltext_index_job.message = (
                    f"Full-text index created with {len(documents)} document(s)."
                )
    except Exception as exc:
        print(f"Full-text index build failed: {exc}", flush=True)
        with _fulltext_index_job_lock:
            _fulltext_index_job.error = f"Failed to create index: {exc}"
    finally:
        with _fulltext_index_job_lock:
            _fulltext_index_job.running = False


def start_fulltext_index_job(vault_path: str) -> bool:
    """Start the background full-text index build; False if one is already running."""
    global _fulltext_index_job
    with _fulltext_index_job_lock:
        if _fulltext_index_job.running:
            return False
        # A fresh instance resets every other field to its dataclass default.
        _fulltext_index_job = FulltextIndexJob(running=True, vault_path=vault_path)

    thread = threading.Thread(
        target=_run_fulltext_index_job,
        args=(vault_path,),
        daemon=True,
    )
    thread.start()
    return True
