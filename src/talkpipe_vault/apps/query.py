"""
Web application for querying and chatting with vault contents.

Provides the following via web interface:
- Vault management: create a new vault or choose an existing one
- Document indexing: add documents to the current vault
- Semantic Search: Vector similarity search returning ranked results
- Keyword Search: Full-text search using a Whoosh index
- Ask: Single-turn RAG-based Q&A interface
- Settings: configure embedding and chat model source/name
"""

import argparse
import errno
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from talkpipe.llm.config import getEmbeddingSources, getPromptSources
from talkpipe.util.config import configure_logger

from talkpipe_vault import DIST_NAME, __version__, memtune
from talkpipe_vault.apps import access_control, credentials, user_settings
from talkpipe_vault.apps.folders import (
    ASK_WAITING,
    FOLDER_CLOSED_PROGRESS,
    FOLDERS_INDEX_THEMSELVES_TEMPLATE,
    FOLDERS_KEEP_EMBEDDER_TEMPLATE,
    FOLDERS_LEFT_NOTE,
    KEYWORD_SEARCH_STARTING,
    SEMANTIC_SEARCH_WAITING,
    SEMANTIC_SEARCH_WAITING_TEMPLATE,
    _folder_banner,
    _folder_chunk,
    _folder_progress_line,
    _folder_source_file,
    _leave_folder_mode,
    _partial_index_note,
    add_folder,
    ask_waiting,
    chunk_text,
    close_folder,
    folders_status,
    is_documents_folder,
    open_closed_folder,
    open_saved_folders,
    rebuild_folder,
    reembed_folder,
    remove_folder,
    resolve_source_file,
    semantic_search_waiting,
    startup_folders,
)
from talkpipe_vault.apps.pipelines import (
    _load_active_filter,
    _refresh_agent_pipelines,
    _refresh_pipelines,
    _update_document_counts,
    init_pipelines,
)
from talkpipe_vault.apps.results import (
    AGENT_FILTER_NOTE,
    AGENT_SEARCH_FILTER_NOTE,
    AGENT_UNAVAILABLE_NOTE,
    _answer_with_agent,
    _answered_by,
    _chat_citations,
    _document_file_response,
    _filter_outcome_note,
    _prefix_note,
    _process_keyword_results,
    _process_semantic_results,
    _select_with_agent,
    _strip_answer_source_paths,
)
from talkpipe_vault.apps.state import (
    DEFAULT_AGENT_SEARCH_ITERATIONS,
    DEFAULT_AGENT_SEARCH_RESULT_COUNT,
    DEFAULT_RAG_RESULT_LIMIT,
    DEFAULT_SEARCH_RESULT_LIMIT,
    EXTRA_EFFORT_ITERATION_FACTOR,
    AppState,
    _apply_vault_embedding_config,
    _clear_pipelines,
    _collect_config_status,
    _effective_models,
    _existing_non_vault_entries,
    _open_folder_indexes,
    _parse_int_setting,
    _state,
    _vault_selected,
    get_state,
    load_saved_model_overrides,
)
from talkpipe_vault.apps.vault_store import (
    _build_whoosh_index,
    _get_chunk_text_for_path_and_snippet,
    _indexed_source_paths,
    _iter_lancedb_docs_for_whoosh,
    _keyword_search_enabled,
    _load_docs_rows,
    _resolve_indexed_source_file,
)
from talkpipe_vault.apps.vaults import (
    _activate_vault,
    _fulltext_index_job_snapshot,
    _index_job_snapshot,
    _is_dangerous_delete_target,
    _nonglob_prefix,
    _record_vault_embedding_config,
    _resolve_source_pattern,
    _resolve_vault_request,
    _retrieval_filter_context,
    _run_index_job,
    _vault_source_folder,
    index_documents_into_vault,
    retrieval_filter_blocker,
    start_fulltext_index_job,
    start_index_job,
    suggest_vault_path,
)
from talkpipe_vault.pipelines import (
    retrieval_filter,
)
from talkpipe_vault.pipelines.config import (
    FOLDER_INDEX_DIRNAME,
)

# Everything the terminal interface, vault-server and the tests reach through
# this module. The implementation lives in the apps modules imported above;
# this module keeps the web routes, page rendering and server startup. The
# process globals rebound at run time (state._folder_scheduler,
# vaults._index_job, vaults._fulltext_index_job) are deliberately not
# re-exported: a copy of a rebound name goes stale.
__all__ = [
    "AGENT_FILTER_NOTE",
    "AGENT_SEARCH_FILTER_NOTE",
    "AGENT_UNAVAILABLE_NOTE",
    "ASK_WAITING",
    "DEFAULT_AGENT_SEARCH_ITERATIONS",
    "DEFAULT_AGENT_SEARCH_RESULT_COUNT",
    "DEFAULT_RAG_RESULT_LIMIT",
    "DEFAULT_SEARCH_RESULT_LIMIT",
    "EXTRA_EFFORT_ITERATION_FACTOR",
    "FOLDERS_INDEX_THEMSELVES_TEMPLATE",
    "FOLDERS_KEEP_EMBEDDER_TEMPLATE",
    "FOLDERS_LEFT_NOTE",
    "FOLDER_CLOSED_PROGRESS",
    "KEYWORD_SEARCH_STARTING",
    "SEMANTIC_SEARCH_WAITING",
    "SEMANTIC_SEARCH_WAITING_TEMPLATE",
    "AppState",
    "_activate_vault",
    "_answer_with_agent",
    "_answered_by",
    "_apply_vault_embedding_config",
    "_browser_url",
    "_build_whoosh_index",
    "_chat_citations",
    "_clear_pipelines",
    "_collect_config_status",
    "_document_file_response",
    "_effective_models",
    "_existing_non_vault_entries",
    "_filter_outcome_note",
    "_folder_banner",
    "_folder_chunk",
    "_folder_progress_line",
    "_folder_source_file",
    "_fulltext_index_job_snapshot",
    "_get_chunk_text_for_path_and_snippet",
    "_index_job_snapshot",
    "_indexed_source_paths",
    "_is_dangerous_delete_target",
    "_iter_lancedb_docs_for_whoosh",
    "_keyword_search_enabled",
    "_launch_browser_when_ready",
    "_leave_folder_mode",
    "_load_active_filter",
    "_load_docs_rows",
    "_nonglob_prefix",
    "_open_folder_indexes",
    "_parse_int_setting",
    "_partial_index_note",
    "_port_in_use",
    "_prefix_note",
    "_process_keyword_results",
    "_process_semantic_results",
    "_record_vault_embedding_config",
    "_refresh_agent_pipelines",
    "_refresh_pipelines",
    "_resolve_indexed_source_file",
    "_resolve_source_pattern",
    "_resolve_vault_request",
    "_retrieval_filter_context",
    "_run_index_job",
    "_running_instance_url",
    "_select_with_agent",
    "_state",
    "_strip_answer_source_paths",
    "_update_document_counts",
    "_vault_selected",
    "_vault_source_folder",
    "add_folder",
    "app",
    "ask_waiting",
    "chunk_text",
    "close_folder",
    "folders_status",
    "get_state",
    "index_documents_into_vault",
    "init_pipelines",
    "is_documents_folder",
    "list_directories",
    "load_saved_model_overrides",
    "main",
    "open_closed_folder",
    "open_saved_folders",
    "rebuild_folder",
    "reembed_folder",
    "remove_folder",
    "resolve_source_file",
    "retrieval_filter_blocker",
    "run_app",
    "semantic_search_waiting",
    "start_fulltext_index_job",
    "start_index_job",
    "startup_folders",
    "suggest_vault_path",
    "templates",
]

configure_logger("root:ERROR")

logger = logging.getLogger(__name__)

# Constants
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Talkpipe Vault", description="Search and chat with your vault")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _template_context(
    request: Request, state: AppState, **extra: Any
) -> dict[str, Any]:
    """Build common template context for vault pages."""
    context = {
        "request": request,
        "vault_path": state.vault_path,
        "shingled_chunks_count": state.shingled_chunks_count,
        "keyword_search_enabled": state.keyword_search_enabled,
        "show_source_paths": state.show_source_paths,
        "result_filter_active": state.result_filter_active,
        "agent_search_available": state.agent_search_pipeline is not None,
        "agent_chat_available": state.agent_chat_pipeline is not None,
        "agent_unavailable_reason": state.agent_unavailable_reason,
        "extra_effort_iterations": (
            state.agent_search_iterations or DEFAULT_AGENT_SEARCH_ITERATIONS
        )
        * EXTRA_EFFORT_ITERATION_FACTOR,
        # Fixed, unlike the configurable Ask/keyword count; pages that mention
        # how many semantic matches they show read it from here.
        "search_result_limit": DEFAULT_SEARCH_RESULT_LIMIT,
        "folder_mode": bool(state.folders),
        "folder_names": [Path(root).name or root for root in state.folders],
        "folder_banner": _folder_banner(state),
        "semantic_search_waiting": semantic_search_waiting(state),
    }
    context.update(extra)
    return context


def _redirect_with_message(url: str, **params: str) -> RedirectResponse:
    """Redirect to a vault page with query-string feedback parameters."""
    filtered = {key: value for key, value in params.items() if value}
    if filtered:
        url = f"{url}?{urllib.parse.urlencode(filtered)}"
    return RedirectResponse(url=url, status_code=303)


def _require_vault(state: AppState) -> RedirectResponse | None:
    """Redirect to the documents page (vault + indexing) when none is selected.

    Carried as a neutral ``message``, not an ``error``: on a first run this is
    the landing page's welcome, and nothing has gone wrong — rendering it in
    the red alert style made the very first screen look like a failure.
    """
    if _vault_selected(state):
        return None
    return _redirect_with_message(
        "/documents", message="Add a folder to search to get started."
    )


def _render_page(
    request: Request,
    state: AppState,
    template: str,
    *,
    require_vault: bool = True,
    **context: Any,
) -> Response:
    """Render a page template with the common context, gating on vault selection."""
    if require_vault:
        redirect = _require_vault(state)
        if redirect:
            return redirect
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=_template_context(request, state, **context),
    )


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Identify this server, without needing a vault.

    Answers ``{"app": "<distribution name>", "version": "<version>"}`` so a
    second ``vault-server`` launch (or a desktop launcher) can tell an
    instance of this application from anything else listening on the port,
    and so monitoring can confirm the server is up.
    """
    return {"app": DIST_NAME, "version": __version__}


@app.get("/api/directories")
async def list_directories(path: str = "") -> JSONResponse:
    """List subdirectories of a path for the folder-picker dialog.

    Returns the resolved path, its parent (null when there is nothing above
    left to browse), and the visible (non-hidden) subdirectories. Falls back
    to the first allowed root — the user's home directory when browsing is
    unrestricted — when no path is given; with several allowed roots the
    virtual top level "" lists the roots themselves as absolute paths.
    Unreadable, nonexistent, or out-of-bounds paths return a 400 with an
    error message the dialog can display.
    """
    roots = access_control.browse_roots()
    requested: str | Path
    if path.strip():
        requested = path
    elif len(roots) > 1:
        return JSONResponse(
            content={
                "path": "",
                "parent": None,
                "home": "",
                "sep": os.sep,
                "directories": [str(root) for root in roots],
            }
        )
    else:
        requested = roots[0] if roots else Path.home()
    # All filesystem access below uses the confined (resolved and
    # root-checked) path, never the request value.
    base = access_control.confine_browse(requested)
    if base is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "Browsing on this server is limited to "
                f"{access_control.describe(roots)}."
            },
        )
    try:
        if not base.is_dir():
            return JSONResponse(
                status_code=400,
                content={"error": f"{base} is not a directory."},
            )
        directories = sorted(
            (
                entry.name
                for entry in base.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            ),
            key=str.casefold,
        )
    except PermissionError:
        return JSONResponse(
            status_code=400,
            content={"error": f"Permission denied reading {base}."},
        )
    except OSError:
        # The exception text can carry filesystem internals; log it and
        # report only the path the client itself asked about.
        logger.exception(f"Failed to list directory {base}")
        return JSONResponse(
            status_code=400,
            content={"error": f"Could not read {base}; details are in the server log."},
        )

    if base in roots:
        # At an allowed root: "up" goes to the virtual root listing when
        # there are several roots, and nowhere when there is only one.
        parent = "" if len(roots) > 1 else None
    else:
        parent = str(base.parent) if base.parent != base else None
    home = (str(roots[0]) if len(roots) == 1 else "") if roots else str(Path.home())
    return JSONResponse(
        content={
            "path": str(base),
            "parent": parent,
            "home": home,
            "sep": os.sep,
            "directories": directories,
        }
    )


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    state: AppState = Depends(get_state),
) -> Response:
    """Render the home page with navigation to search and chat."""
    redirect = _require_vault(state)
    if redirect:
        return redirect
    # Refresh document counts on home page load
    _update_document_counts(state.vault_path)
    return _render_page(
        request,
        state,
        "home.html",
        require_vault=False,
        flash_message=message,
        flash_error=error,
    )


@app.get("/vaults", response_class=HTMLResponse)
async def vaults_page(
    message: str | None = None,
    error: str | None = None,
    confirm_path: str | None = None,
) -> RedirectResponse:
    """Redirect the former vault manager to the combined documents page.

    Choosing a vault and indexing documents now live on one page; this alias
    keeps bookmarks and older links working.
    """
    return _redirect_with_message(
        "/documents",
        message=message or "",
        error=error or "",
        confirm_path=confirm_path or "",
    )


@app.post("/vaults/open", response_class=HTMLResponse)
async def open_vault(
    new_vault_path: Annotated[str, Form()] = "",
    confirm_non_vault: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Open an existing vault, or create a new one when the path doesn't exist.

    Expects form data with:
        - new_vault_path: str - Directory for the vault (created if missing)
        - confirm_non_vault: str - "yes" to confirm creating a vault inside a
          non-empty folder that holds no vault data
    """
    vault_path, placement_note, error = _resolve_vault_request(new_vault_path)
    if vault_path is None:
        return _redirect_with_message("/documents", error=error)

    # A documents folder searched in folder mode holds its index in
    # .tpvault; opening it opens the folder, never a vault among its files.
    if (vault_path / FOLDER_INDEX_DIRNAME).is_dir():
        message, error = add_folder(state, str(vault_path))
        return _redirect_with_message("/documents", message=message, error=error)

    created = not vault_path.is_dir()

    # A folder of ordinary files (e.g. the user's documents folder) is easy to
    # confuse with a vault folder — the vault and documents fields sit on the
    # same page. Opening it as a vault writes index scaffolding alongside the
    # user's files, and deleting the vault later removes the whole folder, so
    # nothing is touched until the user explicitly confirms.
    non_empty_non_vault = _existing_non_vault_entries(vault_path) > 0
    if non_empty_non_vault and confirm_non_vault != "yes":
        return _redirect_with_message("/documents", confirm_path=str(vault_path))

    had_folders = bool(state.folders)
    error = _activate_vault(vault_path)
    if error:
        return _redirect_with_message("/documents", error=error)
    if had_folders:
        placement_note += FOLDERS_LEFT_NOTE

    if created:
        return _redirect_with_message(
            "/documents",
            message=(
                f"Created new vault at {vault_path}. "
                "Add documents to make it searchable." + placement_note
            ),
        )
    if non_empty_non_vault:
        return _redirect_with_message(
            "/documents",
            message=(
                f"Started a new vault at {vault_path}. Note: this folder "
                "already contains files that are not vault data, and index "
                "files will be created alongside them. If you meant to search "
                "those documents, keep the vault elsewhere and index this "
                "folder from this page instead." + placement_note
            ),
        )
    return _redirect_with_message(
        "/", message=f"Opened vault at {vault_path}.{placement_note}"
    )


@app.post("/vaults/delete", response_class=HTMLResponse)
async def delete_vault(
    vault_path: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Forget a vault and delete its folder (index files) from disk.

    Destructive and irreversible. Only vaults already in the recent list can be
    deleted, never the currently-open vault, and an explicit confirm field is
    required.

    Expects form data with:
        - vault_path: str - The recent-vault path to delete
        - confirm: str - Must equal "delete" to proceed
    """
    raw_path = vault_path.strip()
    if not raw_path:
        return _redirect_with_message("/documents", error="No vault specified.")
    if confirm != "delete":
        return _redirect_with_message("/documents", error="Deletion was not confirmed.")

    # Pure string expansion; the recent-list membership check below compares
    # strings, and deletion itself only sees the confined path.
    resolved = os.path.expanduser(raw_path)

    # Only vaults the user already knows about are deletable — never an
    # arbitrary path supplied to this endpoint.
    if resolved not in user_settings.get_recent_vaults():
        return _redirect_with_message(
            "/documents", error="That vault is not in the recent list."
        )

    # Refuse to delete the vault currently open in the app.
    if state.vault_path and os.path.realpath(state.vault_path) == os.path.realpath(
        resolved
    ):
        return _redirect_with_message(
            "/documents",
            error="Cannot delete the vault that is currently open. "
            "Open a different vault first.",
        )

    # Vaults outside the configured root (e.g. remembered before the
    # restriction existed) can be forgotten, but their files are never
    # touched. The deletion below only ever sees the confined (resolved and
    # root-checked) path.
    real_path = access_control.confine_vault(resolved)
    if real_path is None:
        user_settings.forget_vault(resolved)
        return _redirect_with_message(
            "/documents",
            message=(
                f"Removed {resolved} from the list. It is outside "
                f"{access_control.vault_root()}, so its files were left untouched."
            ),
        )

    if _is_dangerous_delete_target(real_path):
        return _redirect_with_message(
            "/documents",
            error=f"Refusing to delete {resolved}: path is too broad to be a vault.",
        )

    existed = real_path.is_dir()
    try:
        if existed:
            shutil.rmtree(real_path)
    except OSError as exc:
        return _redirect_with_message(
            "/documents", error=f"Failed to delete {resolved}: {exc}"
        )

    user_settings.forget_vault(resolved)
    if existed:
        message = f"Deleted vault {resolved} and removed it from the list."
    else:
        message = (
            f"Removed {resolved} from the list (its folder was already gone from disk)."
        )
    return _redirect_with_message("/documents", message=message)


@app.get("/documents", response_class=HTMLResponse)
async def documents_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    confirm_path: str | None = None,
    source_path: str | None = None,
    overwrite: bool = False,
    state: AppState = Depends(get_state),
) -> Response:
    """Render the combined page for choosing a vault and indexing documents.

    Works with or without an open vault: without one, the vault field carries a
    suggested name derived from the chosen documents folder, so a single submit
    creates the vault and indexes into it.
    """
    # Suggest an example path the server will actually accept: with a vault
    # root configured (e.g. in the container, where ~ is ephemeral and outside
    # the fence), point under the root instead of the home directory.
    root = access_control.vault_root()
    vault_example = str(root / "my-vault") if root else "~/my-vault"
    # Re-count instead of trusting a count in the URL; if the folder no longer
    # needs confirmation (emptied, deleted, or now a vault), drop the panel.
    # confirm_path is request data, so the count runs on the confined
    # (resolved and root-checked) path; a path outside the vault fence could
    # never be opened anyway, so it gets no confirmation panel.
    confirmed_vault = (
        access_control.confine_vault(confirm_path) if confirm_path else None
    )
    confirm_entry_count = (
        _existing_non_vault_entries(confirmed_vault)
        if confirmed_vault is not None
        else 0
    )
    return _render_page(
        request,
        state,
        "documents.html",
        require_vault=False,
        models=_effective_models(state),
        recent_vaults=user_settings.get_recent_vaults(),
        folders=folders_status(state),
        recent_folders=user_settings.get_recent_folders(),
        vault_source_folder=_vault_source_folder(state),
        vault_example=vault_example,
        confirm_vault_path=str(confirmed_vault) if confirm_entry_count else None,
        confirm_entry_count=confirm_entry_count,
        pending_source_path=source_path or "",
        pending_overwrite=overwrite,
        retrieval_filter=_retrieval_filter_context(state),
        flash_message=message,
        flash_error=error,
    )


def _render_documents_with_filter(
    request: Request, state: AppState, **pending: Any
) -> Any:
    """Re-render the documents page with pending retrieval-filter values."""
    root = access_control.vault_root()
    return _render_page(
        request,
        state,
        "documents.html",
        require_vault=False,
        models=_effective_models(state),
        recent_vaults=user_settings.get_recent_vaults(),
        folders=folders_status(state),
        recent_folders=user_settings.get_recent_folders(),
        vault_source_folder=_vault_source_folder(state),
        vault_example=str(root / "my-vault") if root else "~/my-vault",
        confirm_vault_path=None,
        confirm_entry_count=0,
        pending_source_path="",
        pending_overwrite=False,
        retrieval_filter=_retrieval_filter_context(state, open=True, **pending),
    )


@app.post("/vaults/retrieval-filter", response_class=HTMLResponse)
async def save_retrieval_filter(
    request: Request,
    action: Annotated[str, Form()] = "save",
    script: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
    strict: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Any:
    """Save, validate, or remove the open vault's retrieval filter.

    Expects form data with:
        - action: str - "save", "validate", or "remove"
        - script: str - The ChatterLang filter script text
        - enabled: str - If truthy, run this vault's filter on this machine
        - strict: str - If truthy, a runtime filter failure fails the request
          instead of falling back to unfiltered results

    The script is stored inside the vault (it travels with the vault's data);
    the enabled/strict flags are stored in this machine's user settings, so a
    copied vault never executes a bundled script without local consent.
    """
    if blocker := retrieval_filter_blocker(state):
        return _redirect_with_message("/documents", error=blocker)

    if action == "remove":
        retrieval_filter.remove_script(state.vault_path)
        user_settings.clear_retrieval_filter_flags(state.vault_path)
        _refresh_pipelines(force=True)
        return _redirect_with_message("/documents", message="Retrieval filter removed.")

    pending = {
        "script": script,
        "enabled": bool(enabled),
        "strict": bool(strict),
    }
    if not script.strip():
        return _render_documents_with_filter(
            request,
            state,
            **pending,
            error="Enter a script, or use Remove to delete the saved one.",
        )

    error = retrieval_filter.validate_script(script)
    if action == "validate":
        return _render_documents_with_filter(
            request,
            state,
            **pending,
            error=error,
            message="" if error else "The script compiles.",
        )
    if error:
        return _render_documents_with_filter(request, state, **pending, error=error)

    retrieval_filter.save_script(state.vault_path, script)
    user_settings.set_retrieval_filter_flags(
        state.vault_path, enabled=bool(enabled), strict=bool(strict)
    )
    _refresh_pipelines(force=True)
    outcome = "enabled" if enabled else "saved but not enabled"
    return _redirect_with_message("/documents", message=f"Retrieval filter {outcome}.")


@app.get("/api/suggest-vault")
async def suggest_vault(source: str = "") -> JSONResponse:
    """Suggest a vault path for a documents folder, for the combined form."""
    return JSONResponse(content={"path": suggest_vault_path(source) if source else ""})


@app.get("/api/index-status")
def index_status(state: AppState = Depends(get_state)) -> JSONResponse:
    """Report the state of the background document-indexing job.

    ``folders`` lists the open folders' indexing progress, and ``banner``
    is the still-indexing notice the pages show ("" when all are done).
    """
    content = _index_job_snapshot()
    content["folders"] = folders_status(state)
    content["banner"] = _folder_banner(state)
    return JSONResponse(content=content)


@app.post("/folders/add", response_class=HTMLResponse)
def folders_add(
    folder_path: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Start searching a folder (and everything under it).

    Expects form data with:
        - folder_path: str - The folder to search
    """
    message, error = add_folder(state, folder_path)
    return _redirect_with_message("/documents", message=message, error=error)


@app.post("/folders/remove", response_class=HTMLResponse)
def folders_remove(
    root: Annotated[str, Form()] = "",
    delete_index: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Stop searching a folder, optionally deleting its index.

    Expects form data with:
        - root: str - An open folder
        - delete_index: str - If truthy, also delete the folder's index
    """
    message, error = remove_folder(state, root, delete_index=bool(delete_index))
    return _redirect_with_message("/documents", message=message, error=error)


@app.post("/folders/close", response_class=HTMLResponse)
def folders_close(
    root: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Stop searching a folder but keep it in the list, its index intact."""
    message, error = close_folder(state, root)
    return _redirect_with_message("/documents", message=message, error=error)


@app.post("/folders/open", response_class=HTMLResponse)
def folders_open(
    root: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Search a closed folder again."""
    message, error = open_closed_folder(state, root)
    return _redirect_with_message("/documents", message=message, error=error)


@app.post("/folders/reembed", response_class=HTMLResponse)
def folders_reembed(
    root: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Rebuild an open folder's vectors with the Settings embedder."""
    message, error = reembed_folder(state, root)
    return _redirect_with_message("/documents", message=message, error=error)


@app.post("/folders/rebuild", response_class=HTMLResponse)
def folders_rebuild(
    root: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Re-index an open folder with the Settings chunking."""
    message, error = rebuild_folder(state, root)
    return _redirect_with_message("/documents", message=message, error=error)


@app.get("/api/fulltext-index-status")
async def fulltext_index_status() -> JSONResponse:
    """Report the state of the background full-text index build."""
    return JSONResponse(content=_fulltext_index_job_snapshot())


@app.get("/api/config-status")
def config_status(
    probe: bool = True,
    download: bool = False,
    state: AppState = Depends(get_state),
) -> JSONResponse:
    """Verify the selected providers are reachable and their credentials set.

    Only the providers referenced by the current embedding/chat selection are
    tested. Defined as a sync endpoint so its blocking network probes run in a
    threadpool rather than on the event loop. Pass ``?probe=0`` to skip the
    live connectivity/credential calls and report only what is known locally.
    Pass ``?download=1`` (the Re-test button) to also let an uncached
    in-process embedding model be downloaded and exercised; the passive
    page-load check never downloads.
    """
    return JSONResponse(
        content=_collect_config_status(state, probe=probe, allow_download=download)
    )


def _open_vault_for_indexing(
    new_vault_path: str,
    pattern: str,
    *,
    source: str,
    confirm_non_vault: str,
    overwrite: bool,
    state: AppState,
) -> tuple[RedirectResponse | None, str]:
    """Open (creating if needed) the vault an indexing run was aimed at.

    Returns (redirect, note): a redirect back to the documents page when the
    vault cannot be used or needs confirmation, otherwise None and a sentence
    for the success message. The pending documents folder rides along on every
    redirect so the form comes back filled in.
    """
    vault_path, placement_note, error = _resolve_vault_request(new_vault_path)
    if vault_path is None:
        return (
            _redirect_with_message("/documents", error=error, source_path=source),
            "",
        )

    # The vault stores the index, not the documents; indexing a folder into
    # itself would sweep the index files back in on the next run.
    if os.path.realpath(vault_path) == os.path.realpath(_nonglob_prefix(pattern)):
        return (
            _redirect_with_message(
                "/documents",
                error=(
                    f"The vault cannot be the folder being indexed ({vault_path}). "
                    "The vault holds the search index; choose a different, empty "
                    "folder for it."
                ),
                source_path=source,
            ),
            "",
        )

    # Already open: nothing to create, and re-opening would needlessly rebuild
    # the pipelines (and reload the embedding model).
    if state.vault_path and os.path.realpath(state.vault_path) == os.path.realpath(
        vault_path
    ):
        return None, ""

    created = not vault_path.is_dir()

    # A folder of ordinary files is easy to confuse with a vault folder, and a
    # vault created there interleaves index files with the user's own — and
    # deleting the vault later would take the whole folder. Confirm first.
    if _existing_non_vault_entries(vault_path) > 0 and confirm_non_vault != "yes":
        return (
            _redirect_with_message(
                "/documents",
                confirm_path=str(vault_path),
                source_path=source,
                overwrite="true" if overwrite else "",
            ),
            "",
        )

    had_folders = bool(state.folders)
    error = _activate_vault(vault_path)
    if error:
        return (
            _redirect_with_message("/documents", error=error, source_path=source),
            "",
        )
    if had_folders:
        placement_note += FOLDERS_LEFT_NOTE
    note = (
        f"Created vault {vault_path}.{placement_note} "
        if created
        else f"Indexing into vault {vault_path}.{placement_note} "
    )
    return None, note


@app.post("/documents/index", response_class=HTMLResponse)
async def index_documents(
    source_path: Annotated[str, Form()] = "",
    new_vault_path: Annotated[str, Form()] = "",
    confirm_non_vault: Annotated[str, Form()] = "",
    overwrite: Annotated[bool, Form()] = False,
    state: AppState = Depends(get_state),
) -> Response:
    """Index documents into a vault, creating or switching to it when asked.

    Expects form data with:
        - source_path: str - Folder path or glob pattern of documents to index
        - new_vault_path: str - Vault to index into (created if missing);
          empty means the vault already open
        - confirm_non_vault: str - "yes" to confirm creating a vault inside a
          non-empty folder that holds no vault data
        - overwrite: bool - Replace the existing index instead of adding to it
    """
    source = source_path.strip()
    if not source:
        # A vault with no documents is the "just open this vault" case — the
        # form's submit button says so — not an error.
        if new_vault_path.strip():
            return await open_vault(
                new_vault_path=new_vault_path,
                confirm_non_vault=confirm_non_vault,
                state=state,
            )
        return _redirect_with_message(
            "/documents", error="Enter a folder or glob pattern to index."
        )

    pattern = _resolve_source_pattern(source)

    # Validate the documents before touching the vault, so a mistyped source
    # never leaves a freshly created, empty vault behind. The existence check
    # runs on the confined (resolved and root-checked) prefix, never the raw
    # request value.
    doc_roots = access_control.document_roots()
    confined_prefix = access_control.confine(_nonglob_prefix(pattern), doc_roots)
    if confined_prefix is None:
        return _redirect_with_message(
            "/documents",
            error="Indexing on this server is limited to documents under "
            f"{access_control.describe(doc_roots)}.",
        )
    # Only a cheap existence check here so typos fail fast; the full source
    # walk (file count + no-match check) runs in the background job, since
    # walking a large tree in this handler would block the event loop and
    # leave the browser hanging on the form submit with no feedback.
    if not confined_prefix.exists():
        return _redirect_with_message(
            "/documents",
            error=(
                f"'{source}' matched no files. Check the folder path or glob pattern."
            ),
        )

    vault_note = ""
    if new_vault_path.strip():
        redirect, vault_note = _open_vault_for_indexing(
            new_vault_path,
            pattern,
            source=source,
            confirm_non_vault=confirm_non_vault,
            overwrite=overwrite,
            state=state,
        )
        if redirect:
            return redirect
    elif state.folders:
        # The open "vault" is a folder's own index, maintained by the
        # folder indexer; a named-vault run must not write into it.
        return _redirect_with_message(
            "/documents",
            error=FOLDERS_INDEX_THEMSELVES_TEMPLATE.format(
                how="choose the vault in Step 2"
            ),
            source_path=source,
        )
    elif not _vault_selected(state):
        return _redirect_with_message(
            "/documents",
            error="Choose a vault to index these documents into.",
            source_path=source,
        )

    models = _effective_models(state)
    started = start_index_job(
        vault_path=state.vault_path,
        source=source,
        pattern=pattern,
        embedding_model=models["embedding_model"],
        embedding_source=models["embedding_source"],
        chunk_size=models["chunk_size"],
        shingle_size=models["shingle_size"],
        shingle_overlap=models["shingle_overlap"],
        overwrite=overwrite,
    )
    if not started:
        return _redirect_with_message(
            "/documents",
            error="An indexing run is already in progress; wait for it to finish.",
        )
    return _redirect_with_message(
        "/documents",
        message=(
            f"{vault_note}Indexing started in the background — progress is shown "
            "below. Embedding with "
            f"{models['embedding_source']}/{models['embedding_model']}."
        ),
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    state: AppState = Depends(get_state),
) -> Response:
    """Render the model configuration page."""
    return _render_page(
        request,
        state,
        "settings.html",
        require_vault=False,
        models=_effective_models(state),
        embedding_sources=getEmbeddingSources(),
        chat_sources=getPromptSources(),
        credentials=credentials.describe(),
        credentials_store=str(credentials.store_path()),
        index_location=user_settings.get_index_location(),
        flash_message=message,
        flash_error=error,
    )


@app.post("/settings/credentials", response_class=HTMLResponse)
async def save_credentials(
    openai_api_key: Annotated[str, Form()] = "",
    clear_openai_api_key: Annotated[bool, Form()] = False,
    openai_base_url: Annotated[str, Form()] = "",
    anthropic_api_key: Annotated[str, Form()] = "",
    clear_anthropic_api_key: Annotated[bool, Form()] = False,
    ollama_server_url: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Save vault-scoped provider credentials and connection settings.

    Non-secret fields (base/server URLs) are set from the form directly — a
    blank value clears them. Secret fields (API keys) are never echoed back, so
    a blank value keeps the saved key and the matching "clear" checkbox removes
    it. Saved values are applied to the process environment immediately.

    URL fields go through the same normalization as the terminal interface: a
    bare ``host:port`` is completed with ``http://`` and the confirmation says
    so, and a URL that can never work is refused here, at the field, instead of
    being stored and then reported by the configuration probe as an unreachable
    server.
    """
    changes: dict[str, str | None] = {
        "openai_base_url": openai_base_url,
        "ollama_server_url": ollama_server_url,
    }
    if clear_openai_api_key:
        changes["openai_api_key"] = ""
    elif openai_api_key.strip():
        changes["openai_api_key"] = openai_api_key
    if clear_anthropic_api_key:
        changes["anthropic_api_key"] = ""
    elif anthropic_api_key.strip():
        changes["anthropic_api_key"] = anthropic_api_key

    try:
        changes, notes = credentials.normalize_url_changes(changes)
    except credentials.UrlCredentialError as exc:
        return _redirect_with_message("/settings", error=str(exc))

    credentials.set_values(changes)
    if _vault_selected(state):
        _refresh_pipelines(force=True)
    return _redirect_with_message(
        "/settings",
        message=" ".join(["Connection settings saved for this app.", *notes]),
    )


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(
    embedding_source: Annotated[str, Form()] = "",
    embedding_model: Annotated[str, Form()] = "",
    chat_source: Annotated[str, Form()] = "",
    chat_model: Annotated[str, Form()] = "",
    chunk_size: Annotated[str, Form()] = "",
    shingle_size: Annotated[str, Form()] = "",
    shingle_overlap: Annotated[str, Form()] = "",
    rag_result_limit: Annotated[str, Form()] = "",
    agent_search_iterations: Annotated[str, Form()] = "",
    agent_search_result_count: Annotated[str, Form()] = "",
    index_location: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> Response:
    """Save model configuration chosen in the web interface.

    Expects form data with:
        - embedding_source / embedding_model: provider and model for embeddings
        - chat_source / chat_model: provider and model for chat/completion
    """
    previous_models = _effective_models(state)
    previous_embedding = (
        previous_models["embedding_source"],
        previous_models["embedding_model"],
    )

    minimums = user_settings.INTEGER_SETTING_MINIMUMS
    try:
        chunk_size_value = _parse_int_setting(
            chunk_size, "Chunk size", minimums["chunk_size"]
        )
        shingle_size_value = _parse_int_setting(
            shingle_size, "Shingle size", minimums["shingle_size"]
        )
        shingle_overlap_value = _parse_int_setting(
            shingle_overlap, "Shingle overlap", minimums["shingle_overlap"]
        )
        rag_result_limit_value = _parse_int_setting(
            rag_result_limit, "Ask result count", minimums["rag_result_limit"]
        )
        agent_iterations_value = _parse_int_setting(
            agent_search_iterations,
            "Agent search iterations",
            minimums["agent_search_iterations"],
        )
        agent_count_value = _parse_int_setting(
            agent_search_result_count,
            "Agent chunk limit",
            minimums["agent_search_result_count"],
        )
        if index_location:
            user_settings.set_index_location(index_location)
    except ValueError as exc:
        return _redirect_with_message("/settings", error=str(exc))

    if (
        shingle_size_value is not None
        and shingle_overlap_value is not None
        and shingle_overlap_value >= shingle_size_value
    ):
        return _redirect_with_message(
            "/settings", error="Shingle overlap must be smaller than shingle size."
        )

    user_settings.save_model_overrides(
        embedding_source=embedding_source,
        embedding_model=embedding_model,
        chat_source=chat_source,
        chat_model=chat_model,
        chunk_size=chunk_size_value,
        shingle_size=shingle_size_value,
        shingle_overlap=shingle_overlap_value,
        rag_result_limit=rag_result_limit_value,
        agent_search_iterations=agent_iterations_value,
        agent_search_result_count=agent_count_value,
    )
    load_saved_model_overrides()
    if _vault_selected(state):
        _refresh_pipelines(force=True)

    models = _effective_models(state)
    message = "Settings saved."
    embedding_changed = previous_embedding != (
        models["embedding_source"],
        models["embedding_model"],
    )
    if embedding_changed and state.folders:
        message += FOLDERS_KEEP_EMBEDDER_TEMPLATE.format(
            action="Re-embed, on Vaults & Documents"
        )
    elif embedding_changed:
        message += (
            " The embedding model changed: existing vaults were indexed with "
            "the previous model, so re-index their documents (with Overwrite) "
            "before searching them."
        )
    return _redirect_with_message("/settings", message=message)


@app.post("/refresh", response_class=HTMLResponse)
async def refresh(
    request: Request,
    return_to: Annotated[str, Form()] = "/",
    state: AppState = Depends(get_state),
) -> RedirectResponse:
    """Refresh pipelines and document counts, then redirect back.

    ``return_to`` comes from the client, so it is only honored when it names
    one of the app's own pages — anything else (an external URL, a
    protocol-relative ``//host``, a backslash variant a browser would treat
    as one) falls back to home. An allow-list, not a prefix check, so no
    crafted value can redirect off-site.
    """
    _refresh_pipelines(force=True)
    _update_document_counts(state.vault_path)

    allowed_returns = (
        "/",
        "/documents",
        "/search",
        "/keyword-search",
        "/chat",
        "/settings",
    )
    # The redirect target is selected *from the constant tuple*, so the
    # request value itself never reaches the redirect.
    target = next((page for page in allowed_returns if page == return_to), "/")

    return RedirectResponse(url=target, status_code=303)


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request, state: AppState = Depends(get_state)
) -> Response:
    """Render the semantic search interface page."""
    return _render_page(request, state, "search.html", query="", results=None)


@app.post("/search", response_class=HTMLResponse)
async def search_results(
    request: Request,
    query: Annotated[str, Form()] = "",
    apply_filter: Annotated[str, Form()] = "",
    use_agent: Annotated[str, Form()] = "",
    use_extra_effort: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """
    Process a semantic search query and return results.

    Expects form data with:
        - query: str - The search query string
        - apply_filter: str - If truthy, run the vault's retrieval filter over
          the results (only honored while a filter is active)
        - use_agent: str - If truthy, let the agent select the results
          (only honored while the agent pipeline is available)
        - use_extra_effort: str - If truthy (with use_agent), let the agent
          take EXTRA_EFFORT_ITERATION_FACTOR times as many iterations

    Returns HTML page with search results containing path, filename, snippet, and score.
    """
    results: list[dict[str, Any]] = []
    error: str | None = None
    filter_note = ""
    agent_trace: list[str] = []

    if query.strip() and state.search_pipeline and not semantic_search_waiting(state):
        try:
            # Refresh pipelines to ensure fresh database connections
            _refresh_pipelines()
            # Update document counts to reflect latest state
            _update_document_counts(state.vault_path)
            # Perform search with refreshed pipeline
            if use_agent and state.agent_search_pipeline:
                agent_outcome = _select_with_agent(
                    state, query, extra_effort=bool(use_extra_effort)
                )
                results = _process_semantic_results(agent_outcome.results)
                filter_note = agent_outcome.note
                agent_trace = agent_outcome.trace
                if apply_filter and state.filtered_search_pipeline:
                    # Both boxes are checked: say which one won instead of
                    # ignoring one silently.
                    filter_note += f" {AGENT_SEARCH_FILTER_NOTE}"
            elif apply_filter and state.filtered_search_pipeline:
                raw_results, filter_error = state.filtered_search_pipeline(query)
                results = _process_semantic_results(raw_results)
                filter_note = _filter_outcome_note(len(results), filter_error)
            else:
                raw_results = state.search_pipeline(query)
                results = _process_semantic_results(raw_results)[
                    :DEFAULT_SEARCH_RESULT_LIMIT
                ]
        except Exception as e:
            error = str(e)

    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context=_template_context(
            request,
            state,
            query=query,
            results=results,
            error=error,
            apply_filter=bool(apply_filter),
            use_agent=bool(use_agent),
            use_extra_effort=bool(use_extra_effort),
            filter_note=filter_note,
            agent_trace=agent_trace,
        ),
    )


@app.get("/keyword-search", response_class=HTMLResponse)
async def keyword_search_page(
    request: Request,
    created: str | None = None,
    error: str | None = None,
    state: AppState = Depends(get_state),
) -> Response:
    """Render the keyword search interface page."""
    # Folders build their keyword index in the background: pick it up when it
    # appears rather than showing the page as disabled until some other
    # request refreshes the pipelines.
    _refresh_pipelines()
    return _render_page(
        request,
        state,
        "keyword_search.html",
        query="",
        results=None,
        flash_message=created,
        flash_error=error,
    )


@app.post("/keyword-search", response_class=HTMLResponse)
async def keyword_search_results(
    request: Request,
    query: Annotated[str, Form()] = "",
    apply_filter: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """
    Process a keyword search query and return results.

    Expects form data with:
        - query: str - The keyword search query (Whoosh query syntax)
        - apply_filter: str - If truthy, run the vault's retrieval filter over
          the results (only honored while a filter is active)

    Returns HTML page with search results containing path, filename, snippet, and score.
    """
    results: list[dict[str, Any]] = []
    error: str | None = None
    filter_note = ""

    # Refresh before checking availability so the UI responds to newly added indexes.
    _refresh_pipelines()
    _update_document_counts(state.vault_path)

    if query.strip() and not state.keyword_search_enabled:
        error = (
            KEYWORD_SEARCH_STARTING
            if state.folders
            else "Keyword search is disabled because this vault has no Whoosh index."
        )
    elif query.strip() and state.keyword_search_pipeline:
        try:
            # Perform search with refreshed pipeline
            if apply_filter and state.filtered_keyword_search_pipeline:
                # Filtered results are normalized SearchResult objects (with
                # path/filename renamed to source/title), so they render
                # through the semantic-result processor.
                raw_results, filter_error = state.filtered_keyword_search_pipeline(
                    query
                )
                results = _process_semantic_results(raw_results)
                filter_note = _filter_outcome_note(len(results), filter_error)
            else:
                raw_results = list(state.keyword_search_pipeline(query))
                results = _process_keyword_results(raw_results)
        except Exception as e:
            error = str(e)

    return templates.TemplateResponse(
        request=request,
        name="keyword_search.html",
        context=_template_context(
            request,
            state,
            query=query,
            results=results,
            error=error,
            apply_filter=bool(apply_filter),
            filter_note=filter_note,
        ),
    )


@app.post("/keyword-search/create-index", response_class=HTMLResponse)
async def create_keyword_index(
    state: AppState = Depends(get_state),
) -> Response:
    """Start a background rebuild of the Whoosh full-text index.

    The build runs in a worker thread that publishes progress via
    ``/api/fulltext-index-status``; the Keyword Search page polls it and shows a
    live progress bar, then reloads with the outcome.
    """
    if not state.vault_path:
        return _redirect_with_message(
            "/keyword-search", error="Vault path is not configured."
        )
    if state.folders:
        # Rebuilding from LanceDB would drop the chunks not embedded yet.
        return _redirect_with_message(
            "/keyword-search",
            error="Folders build their keyword index themselves, first.",
        )

    if not start_fulltext_index_job(state.vault_path):
        return _redirect_with_message(
            "/keyword-search",
            error="A full-text index build is already in progress; "
            "wait for it to finish.",
        )

    return RedirectResponse(url="/keyword-search", status_code=303)


@app.get("/chunk-content")
async def chunk_content(
    path: str,
    snippet: str = "",
    state: AppState = Depends(get_state),
) -> JSONResponse:
    """Return full text for a selected search result chunk."""
    if not state.vault_path:
        return JSONResponse(
            status_code=400,
            content={"error": "Vault path is not configured."},
        )

    try:
        full_text = chunk_text(state, path, snippet)
    except Exception:
        # The exception text can carry paths and library internals; it goes
        # to the server log, not the response.
        logger.exception("Failed to load chunk content")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Failed to load chunk content; details are in the server log."
            },
        )

    if not full_text:
        return JSONResponse(
            status_code=404,
            content={"error": "No text found for this chunk."},
        )

    return JSONResponse(content={"path": path, "content": full_text})


@app.get("/source-file")
async def source_file(
    path: str,
    state: AppState = Depends(get_state),
) -> Response:
    """Serve a source file over HTTP when source-path display is enabled."""
    if not state.show_source_paths:
        return JSONResponse(
            status_code=404,
            content={"error": "Source file links are disabled."},
        )

    if state.folders:
        folder_file = _folder_source_file(state, path)
        if folder_file is None:
            return JSONResponse(
                status_code=404,
                content={"error": "Source file is not in the open folders."},
            )
        return FileResponse(folder_file, filename=folder_file.name)

    try:
        allowed_paths = _indexed_source_paths(state.vault_path)
    except Exception:
        logger.exception("Failed to load the vault's indexed source paths")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Failed to verify the source file path; "
                "details are in the server log."
            },
        )

    # The request value only *selects* among the vault's own indexed paths;
    # the path actually served is the matching entry from the index, so no
    # request-controlled path ever reaches the filesystem.
    known_path = next((known for known in allowed_paths if known == path), None)
    if known_path is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Source file is not referenced by this vault."},
        )

    source_path = Path(known_path)
    if not source_path.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": "Source file was not found."},
        )

    return FileResponse(source_path, filename=source_path.name)


@app.get("/open-file")
async def open_file(
    path: str,
    state: AppState = Depends(get_state),
) -> Response:
    """Open the source document behind a search result or Ask citation.

    Accepts the same lookup key as /chunk-content (docs-row id or indexed
    path) and streams the file over HTTP, so it works wherever the browser
    runs — including against a containerized server, where the document only
    exists at the container-side mount path.
    """
    if not state.vault_path:
        return JSONResponse(
            status_code=400,
            content={"error": "Vault path is not configured."},
        )

    try:
        source_path = resolve_source_file(state, path)
    except Exception:
        logger.exception("Failed to look up the source file")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Failed to look up the source file; "
                "details are in the server log."
            },
        )

    if source_path is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Source file is not referenced by this vault."},
        )

    if not source_path.is_file():
        return JSONResponse(
            status_code=404,
            content={
                "error": (
                    f"Source file {source_path.name} was not found on the "
                    "server's filesystem. If the vault was indexed on another "
                    "machine or in a container, the recorded path may not "
                    "exist here; re-index the documents from this server to "
                    "restore the link."
                )
            },
        )

    return _document_file_response(source_path)


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, state: AppState = Depends(get_state)) -> Response:
    """Render the Ask interface page."""
    return _render_page(request, state, "chat.html", messages=[], citations=[])


@app.post("/chat", response_class=HTMLResponse)
async def chat_response(
    request: Request,
    message: Annotated[str, Form()] = "",
    use_keyword_search: Annotated[str, Form()] = "",
    use_agent: Annotated[str, Form()] = "",
    use_extra_effort: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """
    Process a question and return AI-generated response.

    Expects form data with:
        - message: str - The user's question
        - use_keyword_search: str - If truthy, augment retrieval with
          LLM-extracted keyword search over the full-text index
        - use_agent: str - If truthy, let the agent gather the context it
          answers from (only honored while the agent pipeline is available;
          takes precedence over use_keyword_search, whose keyword search the
          agent already has as a tool)
        - use_extra_effort: str - If truthy (with use_agent), let the agent
          take EXTRA_EFFORT_ITERATION_FACTOR times as many iterations

    Returns HTML page with the question and AI-generated answer.
    """
    messages: list[dict[str, str]] = []
    citations: list[dict[str, Any]] = []
    error: str | None = None
    # The agent's tool calls, shown under the answer the way the Search page
    # shows them: an agent run takes far longer than a plain one and picks its
    # own queries, so what it actually did has to be inspectable.
    agent_trace: list[str] = []
    answered_by_agent = False
    # Whether keyword boost was actually applied is invisible in the answer
    # itself (keyword hits are merged and deduplicated against the vector
    # retrieval), which reads as "it ignored my checkbox" (issue #19). The
    # answer's meta line therefore always states the keyword-boost outcome.
    retrieval_note = ""
    # Same reasoning for the vault's retrieval filter (issue #22): filtered
    # retrieval is invisible in the answer, so the meta line states whether
    # the filter ran, fell back, or could not be applied at all.
    filter_error: str | None = None

    waiting = ask_waiting(state) if message.strip() else ""
    if waiting and state.chat_pipeline:
        # Nothing indexed yet: say so instead of a model answer from nothing.
        messages.append({"role": "user", "content": message})
        messages.append({"role": "assistant", "content": waiting})
    elif message.strip() and state.chat_pipeline:
        messages.append({"role": "user", "content": message})

        try:
            # Refresh pipelines to ensure fresh database connections
            _refresh_pipelines()
            # Update document counts to reflect latest state
            _update_document_counts(state.vault_path)
            result: str | dict[str, Any]
            if use_agent and state.agent_chat_pipeline:
                response, citations, agent_trace, retrieval_note = _answer_with_agent(
                    state, message, extra_effort=bool(use_extra_effort)
                )
                answered_by_agent = True
            elif use_keyword_search and state.keyword_chat_pipeline:
                # The keyword-augmented pipeline reports the merged
                # vector+keyword retrieval it actually prompted with, so the
                # displayed chunks match what the model saw instead of a
                # separately run vector-only search.
                result = state.keyword_chat_pipeline(message)
                response = result["response"]
                citations = _process_semantic_results(result["background"])
                filter_error = result.get("filter_error")
                hits = result.get("keyword_hits")
                if hits:
                    retrieval_note = (
                        f"keyword search boosted retrieval ({hits} keyword "
                        f"hit{'s' if hits != 1 else ''} merged into the context)"
                    )
                else:
                    retrieval_note = (
                        "keyword search boosted retrieval "
                        "(no extra keyword hits for this question)"
                    )
            else:
                if use_keyword_search:
                    retrieval_note = (
                        "keyword search is unavailable right now — "
                        "answered from semantic retrieval only"
                    )
                result = state.chat_pipeline(message)
                if isinstance(result, dict):
                    # The filtered plain pipeline reports the retrieval it
                    # prompted with, so the displayed chunks are the filtered
                    # ones rather than a separately run unfiltered search.
                    response = result["response"]
                    citations = _process_semantic_results(result["background"])
                    filter_error = result.get("filter_error")
                else:
                    response = result
                    citations = _chat_citations(state, message)
            if use_agent and not answered_by_agent:
                # Asked for but not built (bad chat configuration): say so
                # rather than quietly returning a plain retrieval answer.
                retrieval_note = _prefix_note(retrieval_note, AGENT_UNAVAILABLE_NOTE)
            if not state.show_source_paths:
                # The RAG answer text ends with a "Sources:" list of absolute
                # paths; hide them unless --show-source-paths was given, to
                # match the search pages.
                response = _strip_answer_source_paths(response)
            messages.append({"role": "assistant", "content": response})
        except Exception as e:
            error = str(e)
            lowered = error.lower()
            # The provider's own message leads with environment variables and
            # config files. In this application the Settings page is the
            # shorter route and needs nothing outside the app, so say that
            # plainly rather than as an afterthought.
            if "ollama" in lowered and ("connect" in lowered or "refused" in lowered):
                error += (
                    " In this app you can set the Ollama server URL on the "
                    "Settings page under Connections & credentials — no "
                    "environment variable needed."
                )
            elif ("openai" in lowered or "anthropic" in lowered) and (
                "api key" in lowered or "api_key" in lowered or "credential" in lowered
            ):
                error += (
                    " In this app you can enter the API key on the Settings "
                    "page under Connections & credentials — no environment "
                    "variable needed."
                )

    if not state.show_source_paths:
        # The citations list is embedded in the page as JSON; keep absolute
        # filesystem paths out of it unless --show-source-paths was given.
        # The UI resolves chunk content through lookup_path, not path.
        citations = [
            {key: value for key, value in citation.items() if key != "path"}
            for citation in citations
        ]

    answered_by = "" if waiting else _answered_by(state)
    if retrieval_note:
        answered_by = f"{answered_by} · {retrieval_note}"
    partial = _partial_index_note(state) if messages and not waiting else ""
    if partial:
        answered_by = f"{answered_by} · {partial}"
    filter_note = ""
    if state.result_filter_active:
        if answered_by_agent:
            filter_note = AGENT_FILTER_NOTE
        elif filter_error:
            filter_note = (
                f"the vault's retrieval filter failed and was skipped ({filter_error})"
            )
        else:
            filter_note = "the vault's retrieval filter was applied"
    elif state.result_filter_error:
        filter_note = (
            "the vault's retrieval filter does not compile and was not applied"
        )
    elif len(state.folders) > 1 and retrieval_filter.load_script(state.vault_path):
        filter_note = "retrieval filters do not apply while several folders are open"
    if filter_note and messages and not waiting:
        answered_by = f"{answered_by} · {filter_note}"

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context=_template_context(
            request,
            state,
            messages=messages,
            citations=citations,
            error=error,
            answered_by=answered_by,
            use_agent=bool(use_agent),
            use_extra_effort=bool(use_extra_effort),
            agent_trace=agent_trace,
        ),
    )


def run_app(
    vault_path: str = "",
    host: str = "127.0.0.1",
    port: int = 8000,
    show_source_paths: bool = False,
    open_browser: bool = True,
    folders: list[str] | None = None,
) -> None:
    """
    Start the web application server.

    Args:
        vault_path: Path to LanceDB created by makevectordatabase. When empty,
            the interface starts on the vault manager page so a vault can be
            created or chosen in the browser.
        host: Host to bind to (default: 127.0.0.1)
        port: Port to listen on (default: 8000)
        show_source_paths: If True, display and serve source file paths in search results.
        open_browser: If True, open the app in a web browser once the server is
            accepting connections. No-op in headless environments.
        folders: Folders to search directly (folder mode); each keeps its own
            index and is indexed in the background. Takes precedence over
            vault_path.
    """
    problems = access_control.startup_errors()
    if problems:
        raise ValueError(" ".join(problems))
    if vault_path and not access_control.vault_path_allowed(vault_path):
        raise ValueError(
            f"Vault path {vault_path} is outside "
            f"{access_control.VAULT_ROOT_ENV} ({access_control.vault_root()})."
        )
    if not access_control.browse_roots() and host not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        print(
            f"Warning: binding to {host} with unrestricted filesystem "
            f"browsing; set {access_control.VAULT_ROOT_ENV} and "
            f"{access_control.DOCUMENT_ROOTS_ENV} to restrict what the web "
            "interface can reach.",
            file=sys.stderr,
        )

    _state.show_source_paths = show_source_paths
    # Apply persisted credentials before building any pipeline so the LLM SDKs
    # and Ollama connection see the vault-scoped values.
    credentials.apply()
    load_saved_model_overrides()
    if folders:
        for problem in open_saved_folders(_state, folders):
            print(f"Warning: {problem}", file=sys.stderr)
    elif vault_path:
        # Start degraded rather than not at all: an embedder that cannot load
        # (e.g. the model is not cached and Hugging Face is unreachable) should
        # not keep the whole server — and its diagnostics — from coming up.
        # ValueError still propagates: it marks deliberate preflight failures
        # (unsupported vault layout) whose message the caller shows the user.
        try:
            init_pipelines(vault_path)
        except ValueError:
            raise
        except Exception as exc:
            print(
                f"Warning: could not open vault at {vault_path}: {exc}\n"
                "Starting without a vault - check Settings -> Configuration "
                "status, then reopen the vault from the Vaults & Documents page.",
                file=sys.stderr,
            )
            _state.vault_path = ""
            _state.search_pipeline = None
            _state.chat_pipeline = None
            _state.keyword_chat_pipeline = None
            _state.keyword_search_pipeline = None
            _state.result_filter_active = False
            _state.filtered_search_pipeline = None
            _state.filtered_keyword_search_pipeline = None
            _state.agent_search_pipeline = None
            _state.agent_chat_pipeline = None
            _state.agent_search_extra_pipeline = None
            _state.agent_chat_extra_pipeline = None
        user_settings.remember_vault(vault_path)
    if open_browser:
        _launch_browser_when_ready(host, port)
    uvicorn.run(app, host=host, port=port)


def _reachable_host(host: str) -> str:
    """Map a bind host to an address a client can actually reach.

    A wildcard bind address (0.0.0.0 / ::) isn't a routable target, so use the
    loopback address instead; any concrete host is used as-is. The wildcard
    literal here is only compared against, never bound to.
    """
    wildcard_hosts = ("", "0.0.0.0", "::")  # nosec B104 - comparison, not a bind
    return "127.0.0.1" if host in wildcard_hosts else host


def _browser_url(host: str, port: int) -> str:
    """Build the URL to open in a browser for the given bind host and port."""
    return f"http://{_reachable_host(host)}:{port}/"


def _port_in_use(host: str, port: int) -> bool:
    """True when something already listens on host:port.

    Every address the host resolves to is probed, because uvicorn binds all
    of them. Resolution failures count as "free" so uvicorn reports them
    itself.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, socktype, proto, _, sockaddr in infos:
        try:
            sock = socket.socket(family, socktype, proto)
        except OSError:
            continue
        with sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(sockaddr)
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    return True
    return False


def _running_instance_url(host: str, port: int, timeout: float = 1.0) -> str | None:
    """URL of a TalkPipe Vault already serving on host:port, else None.

    Asks the ``/api/health`` route and checks the application name it
    reports, so an unrelated program on the port is not mistaken for a
    running instance.
    """
    url = _browser_url(host, port)
    try:
        with urllib.request.urlopen(  # nosec B310 - http URL to a local port
            url + "api/health", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("app") == DIST_NAME:
        return url
    return None


def _launch_browser_when_ready(host: str, port: int, timeout: float = 15.0) -> None:
    """Open the app in a browser once the server accepts connections.

    Waits in a background daemon thread so we never open a dead page before the
    server is up, and so this does not block server startup. Failures (e.g. a
    headless container with no browser) are ignored.
    """
    url = _browser_url(host, port)
    connect_host = _reachable_host(host)

    def _wait_and_open() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((connect_host, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            return  # server never came up; nothing to open
        try:
            webbrowser.open(url)
        except Exception as exc:
            logger.debug("Could not open a browser for %s: %s", url, exc)

    threading.Thread(target=_wait_and_open, daemon=True).start()


def main() -> None:
    """CLI entry point for vault query web application."""
    # Before any worker threads exist: keep LanceDB ingestion memory flat
    # (see talkpipe_vault.memtune).
    memtune.limit_malloc_arenas()
    parser = argparse.ArgumentParser(
        description="Web application for searching and chatting with your vault"
    )
    parser.add_argument(
        "vault_path",
        nargs="?",
        default="",
        help=(
            "Path to LanceDB directory containing the TalkPipe docs table "
            "(e.g., output path from makevectordatabase). When omitted, "
            "create or choose a vault from the web interface."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)",
    )
    parser.add_argument(
        "--show-source-paths",
        action="store_true",
        help=(
            "Show source file paths in search results and enable HTTP links to those "
            "files. Hidden by default."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the app in a web browser on startup.",
    )

    args = parser.parse_args()
    run_app(
        args.vault_path,
        args.host,
        args.port,
        args.show_source_paths,
        open_browser=not args.no_browser,
    )


if __name__ == "__main__":
    main()
