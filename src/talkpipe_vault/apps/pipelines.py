"""Building the search, Ask and agent pipelines for the open vault or folders.

``init_pipelines`` opens a vault; ``_refresh_pipelines`` (throttled to every
five seconds unless forced) rebuilds every pipeline on the state from the
indexes in play, the retrieval filter and the agent configuration.
"""

import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from talkpipe.search.lancedb import LanceDBDocumentStore

from talkpipe_vault.apps import user_settings
from talkpipe_vault.apps.state import (
    DEFAULT_AGENT_SEARCH_ITERATIONS,
    DEFAULT_AGENT_SEARCH_RESULT_COUNT,
    DEFAULT_RAG_RESULT_LIMIT,
    DEFAULT_SEARCH_RESULT_LIMIT,
    EXTRA_EFFORT_ITERATION_FACTOR,
    _apply_vault_embedding_config,
    _index_paths,
    _search_target,
    _state,
)
from talkpipe_vault.apps.vault_store import _keyword_search_enabled
from talkpipe_vault.pipelines import agent_search, folder_index, retrieval_filter
from talkpipe_vault.pipelines.config import (
    DEFAULT_VECTOR_TABLE_NAME,
    ensure_supported_vault_layout,
    get_vector_db_path,
)
from talkpipe_vault.pipelines.search_results import (
    SEARCH_RESULT_RENAMES,
    SearchResultFilter,
)
from talkpipe_vault.pipelines.vault_chat import VaultChat
from talkpipe_vault.pipelines.vault_search import VaultSearch, VaultTextSearch

logger = logging.getLogger(__name__)

# Where the agent's trace lines go besides stdout, for the run that set it:
# the Ask page shows its run's latest step while it works (issue #107). A
# context variable rather than the segment's progress callable, because one
# segment serves every Search and Ask run; Strands carries the context into
# the threads its tools and hooks run in, so a line reaches only its own run.
agent_progress: ContextVar[Callable[[str], None] | None] = ContextVar(
    "agent_progress", default=None
)


def _agent_progress(message: str) -> None:
    print(message, flush=True)
    sink = agent_progress.get()
    if sink is not None:
        sink(message)


def init_pipelines(vault_path: str) -> None:
    """
    Initialize the search and chat pipelines.

    This function modifies the global application state to set up pipelines
    for semantic search, keyword search, and RAG-based chat.

    Args:
        vault_path: Path to LanceDB created by makevectordatabase.
    """
    ensure_supported_vault_layout(vault_path)
    _state.vault_path = vault_path
    _state.vault_source_folder = None
    _apply_vault_embedding_config(vault_path)
    # Opening a vault must always rebuild the pipelines — the 5-second throttle
    # would otherwise leave the previous vault's state (including
    # keyword_search_enabled) in place when switching vaults in quick succession.
    _refresh_pipelines(force=True)

    # Get document counts from storage locations
    _update_document_counts(vault_path)


def _load_active_filter(vault_path: str) -> str | None:
    """Resolve the vault's retrieval filter and record its state on the app.

    Returns the script text only when it should actually run: present in the
    vault, enabled on this machine for this vault, and compiling cleanly. A
    script that no longer compiles is recorded as an error and *not* applied —
    Ask and the search pages keep working unfiltered, and the state is surfaced
    on the Vaults & Documents page and in the configuration status.
    """
    script = retrieval_filter.load_script(vault_path)
    flags = user_settings.get_retrieval_filter_flags(vault_path)
    _state.result_filter_script = script
    _state.result_filter_strict = flags["strict"]
    _state.result_filter_error = None
    _state.result_filter_active = False
    if not script or not flags["enabled"]:
        return None
    error = retrieval_filter.validate_script(script)
    if error:
        _state.result_filter_error = error
        logger.warning(
            "Retrieval filter for %s is unusable and was not applied: %s",
            vault_path,
            error,
        )
        return None
    _state.result_filter_active = True
    return script


def _refresh_pipelines(force: bool = False) -> None:
    """
    Refresh/reinitialize the pipelines to ensure fresh database connections.

    This is useful when new documents are added to ensure the pipelines
    see the latest data from the databases.

    Args:
        force: If True, always refresh. If False, refresh only if more than
            5 seconds have passed since last refresh (to avoid excessive refreshes).
    """
    vault_path = _state.vault_path
    if not vault_path:
        return

    current_time = time.time()
    # Refresh if forced or if more than 5 seconds have passed
    if not force and (current_time - _state.last_refresh_time) < 5.0:
        return

    target = _search_target(_state)
    filter_script = _load_active_filter(vault_path)
    if isinstance(target, list) and len(target) > 1:
        # A retrieval filter belongs to one vault; with several folders
        # open none applies (the pages say so).
        filter_script = None
        _state.result_filter_active = False
    _state.search_pipeline = VaultSearch(
        vault_path=target,
        embedding_model=_state.embedding_model,
        embedding_source=_state.embedding_source,
        # Serves two readers with different counts: the Search pages, which
        # show DEFAULT_SEARCH_RESULT_LIMIT, and Ask's citation list, which
        # shows the configured Ask result count. Fetch enough for whichever
        # is larger and let each truncate — leaving the limit unset would
        # instead inherit the vector store's own default, so the Search
        # page's result count would be set outside this application.
        limit=max(
            DEFAULT_SEARCH_RESULT_LIMIT,
            _state.rag_result_limit or DEFAULT_RAG_RESULT_LIMIT,
        ),
        # Folders still embedding are keyword-searched too.
        keyword_fallback=bool(_state.folders),
    ).as_function(single_in=True, single_out=True)
    _state.chat_pipeline = VaultChat(
        vault_path=target,
        embedding_model=_state.embedding_model,
        embedding_source=_state.embedding_source,
        chat_model=_state.chat_model,
        chat_source=_state.chat_source,
        limit=_state.rag_result_limit or DEFAULT_RAG_RESULT_LIMIT,
        result_filter_script=filter_script,
        result_filter_strict=_state.result_filter_strict,
        # With a filter, the pipeline reports the retrieval it actually
        # prompted with (and the filter outcome) instead of the answer string.
        include_background=bool(filter_script),
    ).as_function(single_in=True, single_out=True)
    _state.keyword_search_enabled = any(
        _keyword_search_enabled(path) for path in _index_paths(_state)
    )
    if _state.keyword_search_enabled:
        _state.keyword_search_pipeline = VaultTextSearch(
            vault_path=target,
            limit=_state.rag_result_limit or DEFAULT_RAG_RESULT_LIMIT,
        ).as_function(single_in=True, single_out=False)
        try:
            _state.keyword_chat_pipeline = VaultChat(
                vault_path=target,
                embedding_model=_state.embedding_model,
                embedding_source=_state.embedding_source,
                chat_model=_state.chat_model,
                chat_source=_state.chat_source,
                limit=_state.rag_result_limit or DEFAULT_RAG_RESULT_LIMIT,
                keyword_search=True,
                include_background=True,
                result_filter_script=filter_script,
                result_filter_strict=_state.result_filter_strict,
            ).as_function(single_in=True, single_out=True)
        except Exception:
            # Unlike the plain chat pipeline, this one instantiates the chat
            # LLM eagerly (for keyword extraction), which can fail on a bad
            # model configuration. Ask must keep working without the option.
            logger.warning(
                "Could not build the keyword-augmented Ask pipeline",
                exc_info=True,
            )
            _state.keyword_chat_pipeline = None
    else:
        _state.keyword_search_pipeline = None
        _state.keyword_chat_pipeline = None
    _refresh_filtered_search_pipelines(target, filter_script)
    _refresh_agent_pipelines(target)
    _state.last_refresh_time = current_time


def _refresh_agent_pipelines(vault_path: str | list[str]) -> None:
    """(Re)build the agent-driven Search and Ask pipelines.

    One AgentVaultChat backs both: its chunk selection is what the Search page
    shows, and answering from that selection is what Ask uses, so the embedder
    and the agent model are built once per refresh rather than twice.

    Building it resolves the agent's model eagerly, which fails on a bad chat
    configuration; Search and Ask must keep working without the option, so
    failure leaves both pipelines unset and the checkboxes hidden.
    """
    try:
        segment = agent_search.AgentVaultChat(
            vault_path=vault_path,
            embedding_model=_state.embedding_model,
            embedding_source=_state.embedding_source,
            chat_model=_state.chat_model,
            chat_source=_state.chat_source,
            max_chunks=_state.agent_search_result_count
            or DEFAULT_AGENT_SEARCH_RESULT_COUNT,
            max_iterations=_state.agent_search_iterations
            or DEFAULT_AGENT_SEARCH_ITERATIONS,
            progress=_agent_progress,
        )
    except Exception as exc:
        logger.warning("Could not build the agent pipelines", exc_info=True)
        _state.agent_search_pipeline = None
        _state.agent_chat_pipeline = None
        _state.agent_search_extra_pipeline = None
        _state.agent_chat_extra_pipeline = None
        _state.agent_unavailable_reason = str(exc) or type(exc).__name__
        return
    _state.agent_unavailable_reason = ""
    _state.agent_search_pipeline = segment.select_chunks
    _state.agent_chat_pipeline = segment.as_function(single_in=True, single_out=True)
    extra = segment.with_max_iterations(
        segment.max_iterations * EXTRA_EFFORT_ITERATION_FACTOR
    )
    _state.agent_search_extra_pipeline = extra.select_chunks
    _state.agent_chat_extra_pipeline = extra.as_function(
        single_in=True, single_out=True
    )


def _refresh_filtered_search_pipelines(
    vault_path: str | list[str], filter_script: str | None
) -> None:
    """(Re)build the opt-in filtered variants of the search-page pipelines.

    Each variant over-fetches, runs the vault's retrieval filter, truncates
    back to the page's result count, and returns (results, filter_error).
    Built only when a filter is active; the search pages show their "apply
    custom transform" checkbox only when the matching variant exists.
    """
    _state.filtered_search_pipeline = None
    _state.filtered_keyword_search_pipeline = None
    if not filter_script:
        return

    overfetch = retrieval_filter.FILTER_OVERFETCH_FACTOR
    try:
        semantic_limit = DEFAULT_SEARCH_RESULT_LIMIT
        semantic_search = VaultSearch(
            vault_path=vault_path,
            embedding_model=_state.embedding_model,
            embedding_source=_state.embedding_source,
            limit=semantic_limit * overfetch,
        ).as_function(single_in=True, single_out=True)
        semantic_filter = SearchResultFilter(
            script=filter_script,
            field="_background",
            limit=semantic_limit,
            strict=_state.result_filter_strict,
            rename_fields=SEARCH_RESULT_RENAMES,
        )

        def _filtered_semantic(query: str) -> tuple[list[Any], str | None]:
            raw = semantic_search(query)
            if isinstance(raw, dict):
                raw = [raw]
            return semantic_filter.apply(list(raw or []))

        _state.filtered_search_pipeline = _filtered_semantic
    except Exception:
        logger.warning(
            "Could not build the filtered semantic-search pipeline", exc_info=True
        )

    if not _state.keyword_search_enabled:
        return
    try:
        keyword_limit = _state.rag_result_limit or DEFAULT_RAG_RESULT_LIMIT
        keyword_search = VaultTextSearch(
            vault_path=vault_path,
            limit=keyword_limit * overfetch,
        ).as_function(single_in=True, single_out=False)
        keyword_filter = SearchResultFilter(
            script=filter_script,
            field="_background",
            limit=keyword_limit,
            strict=_state.result_filter_strict,
            rename_fields=SEARCH_RESULT_RENAMES,
        )

        def _filtered_keyword(query: str) -> tuple[list[Any], str | None]:
            return keyword_filter.apply(list(keyword_search(query)))

        _state.filtered_keyword_search_pipeline = _filtered_keyword
    except Exception:
        logger.warning(
            "Could not build the filtered keyword-search pipeline", exc_info=True
        )


def _update_document_counts(vault_path: str) -> None:
    """Update document counts from vault storage locations.

    In folder mode the count is the open folders' chunks, embedded or not.
    """
    if _state.folders:
        _state.shingled_chunks_count = sum(
            int(folder_index.read_index_status(path).get("chunks") or 0)
            for path in _index_paths(_state)
        )
        return
    ensure_supported_vault_layout(vault_path)
    vectordb_path = get_vector_db_path(vault_path)

    # Get counts from LanceDB tables
    try:
        doc_store = LanceDBDocumentStore(
            path=vectordb_path,
            table_name=DEFAULT_VECTOR_TABLE_NAME,
        )
        # keyword_search_enabled is owned by _refresh_pipelines, which every
        # caller runs first; only the failure path below overrides it.
        _state.shingled_chunks_count = doc_store.count()
    except Exception:
        _state.shingled_chunks_count = 0
        _state.keyword_search_enabled = False
