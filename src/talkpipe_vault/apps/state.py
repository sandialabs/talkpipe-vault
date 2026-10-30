"""Application state shared by the web and terminal interfaces.

The ``AppState`` singleton with the open vault or folders and the pipelines
built for them, the configuration in effect (interface overrides, then
TalkPipe configuration, then defaults), the process's folder indexer with
the index paths the pipelines search, and the configuration status report.
Nothing here renders a page or builds a pipeline.
"""

import atexit
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from talkpipe.llm.config import getEmbeddingSources, getPromptSources

from talkpipe_vault.apps import user_settings
from talkpipe_vault.apps.vault_store import _vault_has_documents
from talkpipe_vault.pipelines import (
    agent_search,
    diagnostics,
    folder_index,
    retrieval_filter,
    vault_metadata,
)
from talkpipe_vault.pipelines.config import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SHINGLE_OVERLAP,
    DEFAULT_SHINGLE_SIZE,
    existing_non_vault_entries,
    get_chat_model,
    get_chat_source,
    get_embedding_model,
    get_embedding_source,
)

logger = logging.getLogger(__name__)


# Chunks Ask (and keyword search) retrieve when the Settings page sets none.
# With the default 300x3 shingles, 5 chunks often held the right file but not
# the passage the answer needed; 8 answered measurably better on a Q/A benchmark.
DEFAULT_RAG_RESULT_LIMIT = 8
# How many results the Semantic Search page shows. Unlike the Ask/keyword
# count, this is fixed rather than configurable; both the plain and the
# filtered semantic pipelines pass it explicitly, so the page's result count
# is this constant rather than whatever default the vector store happens to
# ship.
DEFAULT_SEARCH_RESULT_LIMIT = 10
DEFAULT_AGENT_SEARCH_ITERATIONS = agent_search.DEFAULT_MAX_ITERATIONS
DEFAULT_AGENT_SEARCH_RESULT_COUNT = agent_search.DEFAULT_MAX_CHUNKS
# Extra Effort multiplies the agent's iteration cap for one question. On the
# vault-tune benchmark doubling it (8 -> 16) lifted a strong tool-calling model
# on multi-step questions and changed little for weaker ones, so it is an
# opt-in per question rather than a higher default.
EXTRA_EFFORT_ITERATION_FACTOR = 2


@dataclass
class AppState:
    """Application state container for vault configuration and pipelines."""

    vault_path: str = ""
    search_pipeline: Callable[[str], Any] | None = None
    # Plain chat returns the answer string, or a result dict when it is built
    # with include_background (filtered retrieval); keyword chat always
    # returns the result dict.
    chat_pipeline: Callable[[str], str | dict[str, Any]] | None = None
    keyword_chat_pipeline: Callable[[str], dict[str, Any]] | None = None
    keyword_search_pipeline: Callable[[str], list[Any]] | None = None
    # Agent-driven retrieval, offered on both search-style pages: an agent
    # runs the vector and keyword searches as tools, keeps/ignores chunks, and
    # stops when it judges it could answer the question from what it kept.
    # agent_search_result_count only bounds how much it may collect, and
    # agent_search_iterations how many turns it may take, if it never does.
    # The Search page shows the chunks it kept; Ask answers
    # from them. Both come from one AgentVaultChat segment (built once, so the
    # embedder and the agent model are not set up twice), and both are None
    # when the agent's model cannot be built — the pages then offer their
    # plain pipelines only.
    agent_search_pipeline: Callable[[str], agent_search.AgentSearchOutcome] | None = (
        None
    )
    agent_chat_pipeline: Callable[[str], agent_search.AgentChatOutcome] | None = None
    # The same two with EXTRA_EFFORT_ITERATION_FACTOR times the iteration cap,
    # behind the pages' Extra Effort checkbox; set and cleared with the above.
    agent_search_extra_pipeline: (
        Callable[[str], agent_search.AgentSearchOutcome] | None
    ) = None
    agent_chat_extra_pipeline: Callable[[str], agent_search.AgentChatOutcome] | None = (
        None
    )
    # Why the agent pipelines could not be built (empty when they were), so
    # the pages can say why the option is missing instead of hiding it
    # without a word.
    agent_unavailable_reason: str = ""
    # Retrieval filter (issue #22): the open vault's script, whether it is
    # active (present + enabled + compiles), and the search-page pipelines
    # that apply it on demand. Each filtered pipeline returns
    # (results, filter_error) where filter_error is None unless the script
    # failed at run time and the unfiltered results were kept.
    result_filter_script: str | None = None
    result_filter_active: bool = False
    result_filter_strict: bool = False
    result_filter_error: str | None = None
    filtered_search_pipeline: Callable[[str], tuple[list[Any], str | None]] | None = (
        None
    )
    filtered_keyword_search_pipeline: (
        Callable[[str], tuple[list[Any], str | None]] | None
    ) = None
    shingled_chunks_count: int = 0
    keyword_search_enabled: bool = False
    show_source_paths: bool = False
    last_refresh_time: float = 0.0
    # Model overrides chosen in the web interface. None falls through to
    # TalkPipe configuration and then the vault defaults.
    embedding_model: str | None = None
    embedding_source: str | None = None
    chat_model: str | None = None
    chat_source: str | None = None
    chunk_size: int | None = None
    shingle_size: int | None = None
    shingle_overlap: int | None = None
    rag_result_limit: int | None = None
    agent_search_iterations: int | None = None
    agent_search_result_count: int | None = None
    # Folder mode: the folders searched directly, each with its own index.
    # While any is open, vault_path is the first folder's index so the
    # single-vault helpers keep working, and the pipelines search them all.
    folders: list[str] = field(default_factory=list)
    # The folder all of the open named vault's documents come from, offered
    # as "search that folder instead"; None until computed for this vault.
    vault_source_folder: str | None = None
    # The web Ask page's question being answered and its last answer, each
    # with the vault or folders it was asked of (issue #106): an agent run
    # takes minutes, and leaving or reloading the page must not lose it.
    # ask_id is the most recent question's number; only its run updates them.
    ask_running: dict[str, Any] | None = None
    last_ask: dict[str, Any] | None = None
    ask_id: int = 0


# Application state singleton
_state = AppState()


def get_state() -> AppState:
    """Dependency that provides access to application state."""
    return _state


def _effective_models(state: AppState) -> dict[str, Any]:
    """Resolve the app configuration in effect (overrides, config, defaults)."""
    return {
        "embedding_model": state.embedding_model or get_embedding_model(),
        "embedding_source": state.embedding_source or get_embedding_source(),
        "chat_model": state.chat_model or get_chat_model(),
        "chat_source": state.chat_source or get_chat_source(),
        "chunk_size": state.chunk_size or DEFAULT_CHUNK_SIZE,
        "shingle_size": state.shingle_size or DEFAULT_SHINGLE_SIZE,
        "shingle_overlap": state.shingle_overlap or DEFAULT_SHINGLE_OVERLAP,
        "rag_result_limit": state.rag_result_limit or DEFAULT_RAG_RESULT_LIMIT,
        "agent_search_iterations": state.agent_search_iterations
        or DEFAULT_AGENT_SEARCH_ITERATIONS,
        "agent_search_result_count": state.agent_search_result_count
        or DEFAULT_AGENT_SEARCH_RESULT_COUNT,
    }


def _drop_unavailable_source(
    source: str | None, model: str | None, role: str, origin: str
) -> tuple[str | None, str | None]:
    """Return (source, model), dropping both when the source is unavailable.

    A persisted provider choice can stop being valid — for example a
    plugin-provided embedder whose package was uninstalled. Building a pipeline
    with it raises, which used to abort server startup entirely; the fix then
    lives on a Settings page that never comes up. Dropping the stale pair (the
    model name is meaningless without its provider) falls back to the TalkPipe
    configuration/defaults so the server boots and the Settings page can flag
    the mismatch.
    """
    if not source:
        return source, model
    available = getEmbeddingSources() if role == "embedding" else getPromptSources()
    if source in available:
        return source, model
    message = (
        f"Warning: saved {role} source '{source}' ({origin}) is not available "
        f"(available: {', '.join(available)}). Falling back to the configured "
        f"default; fix the choice on the Settings page or edit "
        f"{user_settings.settings_file_path()}."
    )
    print(message, file=sys.stderr)
    logger.warning(message)
    return None, None


def load_saved_model_overrides() -> None:
    """Apply model overrides persisted by the settings page to app state."""
    overrides = user_settings.get_model_overrides()
    embedding_source, embedding_model = _drop_unavailable_source(
        overrides.get("embedding_source"),
        overrides.get("embedding_model"),
        "embedding",
        "saved settings",
    )
    chat_source, chat_model = _drop_unavailable_source(
        overrides.get("chat_source"),
        overrides.get("chat_model"),
        "chat",
        "saved settings",
    )
    _state.embedding_model = embedding_model
    _state.embedding_source = embedding_source
    _state.chat_model = chat_model
    _state.chat_source = chat_source
    _state.chunk_size = overrides.get("chunk_size")
    _state.shingle_size = overrides.get("shingle_size")
    _state.shingle_overlap = overrides.get("shingle_overlap")
    _state.rag_result_limit = overrides.get("rag_result_limit")
    _state.agent_search_iterations = overrides.get("agent_search_iterations")
    _state.agent_search_result_count = overrides.get("agent_search_result_count")


def _parse_int_setting(value: str, label: str, minimum: int) -> int | None:
    """Parse an integer setting from form data, allowing blank values."""
    text = value.strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if number < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    return number


_folder_scheduler: folder_index.FolderScheduler | None = None
_folder_scheduler_lock = threading.Lock()


def _scheduler() -> folder_index.FolderScheduler:
    """The process's folder indexer, started on first use."""
    global _folder_scheduler
    with _folder_scheduler_lock:
        if _folder_scheduler is None:
            _folder_scheduler = folder_index.FolderScheduler(
                ingest_lock_dir=user_settings.get_vault_home()
            )
            _folder_scheduler.start()
            # Finish the batch in progress and release the folders on exit;
            # whatever is left resumes from the manifest next time.
            atexit.register(_folder_scheduler.stop)
        return _folder_scheduler


def _open_folder_indexes(state: AppState) -> list[folder_index.FolderIndex]:
    """The open folders' indexes, in the order the folders were added."""
    if _folder_scheduler is None or not state.folders:
        return []
    by_root = {str(f.root): f for f in _folder_scheduler.folders()}
    return [by_root[root] for root in state.folders if root in by_root]


def _index_paths(state: AppState) -> list[str]:
    """Every index the pipelines search: the open folders', else the vault."""
    if state.folders:
        return [f.index_path for f in _open_folder_indexes(state)]
    return [state.vault_path] if state.vault_path else []


def _search_target(state: AppState) -> str | list[str]:
    """The vault_path the search segments get (a list in folder mode)."""
    return _index_paths(state) if state.folders else state.vault_path


def _clear_pipelines(state: AppState) -> None:
    state.search_pipeline = None
    state.chat_pipeline = None
    state.keyword_chat_pipeline = None
    state.keyword_search_pipeline = None
    state.keyword_search_enabled = False
    state.result_filter_active = False
    state.filtered_search_pipeline = None
    state.filtered_keyword_search_pipeline = None
    state.agent_search_pipeline = None
    state.agent_chat_pipeline = None
    state.agent_search_extra_pipeline = None
    state.agent_chat_extra_pipeline = None
    state.shingled_chunks_count = 0


def _apply_vault_embedding_config(vault_path: str | None) -> None:
    """Set the active embedder to the one the vault was indexed with.

    Embeddings are only comparable to a query embedded with the same model, so on
    open we restore the vault's recorded embedder. Legacy vaults with no record
    keep the user's saved override / default unchanged (the same behavior as
    before this was tracked) — resolved from the saved overrides rather than from
    whatever a previously opened vault left behind — and the mismatch is surfaced
    on the Settings page's configuration status. With no vault path the saved
    override / default is applied, as for a legacy vault.
    """
    recorded = vault_metadata.load_embedding_config(vault_path) if vault_path else None
    saved = user_settings.get_model_overrides()
    saved_source, saved_model = _drop_unavailable_source(
        saved.get("embedding_source"),
        saved.get("embedding_model"),
        "embedding",
        "saved settings",
    )
    recorded_source = recorded.get("source") if recorded else None
    recorded_model = recorded.get("model") if recorded else None
    if recorded_source:
        recorded_source, recorded_model = _drop_unavailable_source(
            recorded_source,
            recorded_model,
            "embedding",
            "recorded in the vault's vault_metadata.json",
        )
    if recorded_source or recorded_model:
        _state.embedding_source = recorded_source or saved_source
        _state.embedding_model = recorded_model or saved_model
    else:
        _state.embedding_source = saved_source
        _state.embedding_model = saved_model


def _vault_selected(state: AppState) -> bool:
    """Return True when a vault is currently selected."""
    return bool(state.vault_path)


# The check lives with the vault layout helpers so pipelines can use it; the
# old name stays for the callers here and in the terminal interface.
_existing_non_vault_entries = existing_non_vault_entries


def _collect_config_status(
    state: AppState, probe: bool = True, allow_download: bool = False
) -> dict[str, Any]:
    """Build the config-status report for the effective model selection."""
    vault_selected = _vault_selected(state)
    vault_embedding = (
        vault_metadata.load_embedding_config(state.vault_path)
        if vault_selected
        else None
    )
    filter_info = None
    if vault_selected:
        script = retrieval_filter.load_script(state.vault_path)
        if script:
            flags = user_settings.get_retrieval_filter_flags(state.vault_path)
            # The compiler's message can quote exception text, which does not
            # belong in this API response (information exposure through an
            # exception); the status only says *that* the script is broken.
            # The full message is where scripts get fixed: the vault's
            # retrieval-filter editor (Validate).
            filter_info = {
                "enabled": flags["enabled"],
                "strict": flags["strict"],
                "error": (
                    "The filter script does not compile. Open the vault's "
                    "retrieval filter editor and press Validate to see the "
                    "compiler message."
                    if retrieval_filter.validate_script(script)
                    else None
                ),
            }
    return diagnostics.collect_config_status(
        _effective_models(state),
        vault_selected=vault_selected,
        vault_embedding=vault_embedding,
        vault_indexed=vault_selected and _vault_has_documents(state.vault_path),
        retrieval_filter=filter_info,
        probe=probe,
        allow_download=allow_download,
    )
