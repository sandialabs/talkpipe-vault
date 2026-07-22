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
import glob as globlib
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable

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
from talkpipe.pipelines.vector_databases import (
    EmbedderPreflightError,
    EmbeddingDimensionMismatchError,
    RagIngestError,
    build_rag_database,
)
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshFullTextIndex
from talkpipe.util.config import configure_logger

from talkpipe_vault.apps import access_control, credentials, user_settings
from talkpipe_vault.pipelines import diagnostics, vault_metadata
from talkpipe_vault.pipelines.config import (
    DEFAULT_VECTOR_TABLE_NAME,
    ensure_supported_vault_layout,
    get_chat_model,
    get_chat_source,
    get_embedding_model,
    get_embedding_source,
    get_retrieval_template,
    get_vector_db_path,
    get_whoosh_index_path,
)
from talkpipe_vault.pipelines.searching_and_prompting import (
    VaultChat,
    VaultSearch,
    VaultTextSearch,
)

configure_logger("root:ERROR")

logger = logging.getLogger(__name__)

# Constants
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
SNIPPET_MAX_LENGTH = 300
CHAT_CITATION_LIMIT = 5
DEFAULT_CHUNK_SIZE = 300
DEFAULT_SHINGLE_SIZE = 3
DEFAULT_SHINGLE_OVERLAP = 1
DEFAULT_RAG_RESULT_LIMIT = CHAT_CITATION_LIMIT


@dataclass
class AppState:
    """Application state container for vault configuration and pipelines."""

    vault_path: str = ""
    search_pipeline: Callable[[str], Any] | None = None
    chat_pipeline: Callable[[str], str] | None = None
    keyword_search_pipeline: Callable[[str], list[Any]] | None = None
    shingled_chunks_count: int = 0
    fulltext_documents_count: int = 0
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


# Application state singleton
_state = AppState()

app = FastAPI(title="Talkpipe Vault", description="Search and chat with your vault")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_state() -> AppState:
    """Dependency that provides access to application state."""
    return _state


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
    }
    context.update(extra)
    return context


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


def _redirect_with_message(url: str, **params: str) -> RedirectResponse:
    """Redirect to a vault page with query-string feedback parameters."""
    filtered = {key: value for key, value in params.items() if value}
    if filtered:
        url = f"{url}?{urllib.parse.urlencode(filtered)}"
    return RedirectResponse(url=url, status_code=303)


def _keyword_search_enabled(vault_path: str) -> bool:
    """Return True when the vault has a readable Whoosh full-text index."""
    if not vault_path:
        return False

    whoosh_index_path = get_whoosh_index_path(vault_path)

    try:
        with WhooshFullTextIndex(whoosh_index_path):
            return True
    except Exception:
        return False


def _iter_lancedb_docs_for_whoosh(vault_path: str) -> list[dict[str, str]]:
    """
    Read docs-table records from LanceDB and normalize for Whoosh indexing.

    Expects rows where `document` is either JSON text or a dict containing content/path/title.
    """
    vectordb_path = get_vector_db_path(vault_path)
    ensure_supported_vault_layout(vault_path)
    rows: list[dict[str, Any]] = []
    doc_store = LanceDBDocumentStore(
        path=vectordb_path,
        table_name=DEFAULT_VECTOR_TABLE_NAME,
    )
    table, _ = doc_store._get_table()
    if hasattr(table, "to_arrow"):
        rows = table.to_arrow().to_pylist()
    elif hasattr(table, "to_pandas"):
        rows = table.to_pandas().to_dict(orient="records")
    else:
        raise RuntimeError(
            "Unsupported LanceDB table reader: expected to_arrow() or to_pandas()."
        )

    documents: list[dict[str, str]] = []
    for row in rows:
        raw_doc = row.get("document", {})
        if isinstance(raw_doc, str):
            try:
                parsed_doc = json.loads(raw_doc)
            except json.JSONDecodeError:
                parsed_doc = {"content": raw_doc}
        elif isinstance(raw_doc, dict):
            parsed_doc = raw_doc
        else:
            parsed_doc = {"content": str(raw_doc)}

        path = str(
            parsed_doc.get("path")
            or parsed_doc.get("source")
            or parsed_doc.get("id")
            or row.get("id", "")
        )
        filename = str(
            parsed_doc.get("filename")
            or parsed_doc.get("title")
            or (Path(path).name if path else "")
        )
        documents.append(
            {
                "doc_id": str(row.get("id", path)),
                "content": str(parsed_doc.get("content", "")),
                "path": path,
                "filename": filename,
            }
        )

    return documents


WHOOSH_INDEX_FIELDS = ["content", "path", "filename"]


def _build_whoosh_index(
    vault_path: str,
    documents: list[dict[str, str]],
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Rebuild the vault's Whoosh full-text index from normalized documents.

    Expects documents as dicts with doc_id, content, path, and filename keys.
    The existing index is replaced. Documents keep their LanceDB row ids as
    Whoosh doc_ids so search results can be resolved back to stored chunks.
    (talkpipe's indexWhoosh segment reserves the doc_id schema field, so the
    index is built through WhooshFullTextIndex to control ids directly.)
    The optional progress callback receives (docs_done, total_docs) after each
    document is added.
    """
    whoosh_index_path = get_whoosh_index_path(vault_path)
    if os.path.isdir(whoosh_index_path):
        shutil.rmtree(whoosh_index_path)

    total = len(documents)
    with WhooshFullTextIndex(whoosh_index_path, fields=WHOOSH_INDEX_FIELDS) as ix:
        # One writer committed once for the whole rebuild: add_document commits
        # per call, which makes Whoosh re-merge its segments on every document
        # and degrades quadratically with vault size.
        with ix.ix.writer() as writer:
            for done, document in enumerate(documents, start=1):
                writer.update_document(
                    doc_id=document.get("doc_id") or str(uuid.uuid4()),
                    **{
                        field: str(document.get(field, ""))
                        for field in WHOOSH_INDEX_FIELDS
                    },
                )
                if progress is not None:
                    progress(done, total)


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
    _apply_vault_embedding_config(vault_path)
    _refresh_pipelines()

    # Get document counts from storage locations
    _update_document_counts(vault_path)


def _apply_vault_embedding_config(vault_path: str) -> None:
    """Set the active embedder to the one the vault was indexed with.

    Embeddings are only comparable to a query embedded with the same model, so on
    open we restore the vault's recorded embedder. Legacy vaults with no record
    keep the user's saved override / default unchanged (the same behavior as
    before this was tracked) — resolved from the saved overrides rather than from
    whatever a previously opened vault left behind — and the mismatch is surfaced
    on the Settings page's configuration status.
    """
    recorded = vault_metadata.load_embedding_config(vault_path)
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


def _refresh_pipelines(force: bool = False) -> None:
    """
    Refresh/reinitialize the pipelines to ensure fresh database connections.

    This is useful when new documents are added to ensure the pipelines
    see the latest data from the databases.

    Args:
        force: If True, always refresh. If False, refresh only if more than
            5 seconds have passed since last refresh (to avoid excessive refreshes).
            Set to False to disable automatic refresh (for testing).
    """
    import time

    vault_path = _state.vault_path
    if not vault_path:
        return

    # For testing: can disable refresh by setting environment variable
    # This allows us to verify the problem exists without the fix
    if os.environ.get("DISABLE_PIPELINE_REFRESH", "").lower() == "true":
        return

    current_time = time.time()
    # Refresh if forced or if more than 5 seconds have passed
    if not force and (current_time - _state.last_refresh_time) < 5.0:
        return

    _state.search_pipeline = VaultSearch(
        vault_path=vault_path,
        embedding_model=_state.embedding_model,
        embedding_source=_state.embedding_source,
    ).as_function(single_in=True, single_out=True)
    _state.chat_pipeline = VaultChat(
        vault_path=vault_path,
        embedding_model=_state.embedding_model,
        embedding_source=_state.embedding_source,
        chat_model=_state.chat_model,
        chat_source=_state.chat_source,
        limit=_effective_models(_state)["rag_result_limit"],
    ).as_function(single_in=True, single_out=True)
    _state.keyword_search_enabled = _keyword_search_enabled(vault_path)
    if _state.keyword_search_enabled:
        _state.keyword_search_pipeline = VaultTextSearch(
            vault_path=vault_path
        ).as_function(single_in=True, single_out=False)
    else:
        _state.keyword_search_pipeline = None
    _state.last_refresh_time = current_time


def _update_document_counts(vault_path: str) -> None:
    """Update document counts from vault storage locations."""
    ensure_supported_vault_layout(vault_path)
    vectordb_path = get_vector_db_path(vault_path)

    # Get counts from LanceDB tables
    try:
        doc_store = LanceDBDocumentStore(
            path=vectordb_path,
            table_name=DEFAULT_VECTOR_TABLE_NAME,
        )
        docs_count = doc_store.count()
        _state.keyword_search_enabled = _keyword_search_enabled(vault_path)
        _state.shingled_chunks_count = docs_count
        _state.fulltext_documents_count = 0
    except Exception:
        _state.shingled_chunks_count = 0
        _state.fulltext_documents_count = 0
        _state.keyword_search_enabled = False


def _get_field(obj: Any, field: str, default: Any = "") -> Any:
    """Extract a field from either a dict or object."""
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


def _create_snippet(text: str, max_length: int = SNIPPET_MAX_LENGTH) -> str:
    """Create a truncated snippet from text content."""
    snippet = text[:max_length].replace("\n", " ").strip()
    if len(text) > max_length:
        snippet += "..."
    return snippet


def _resolve_title(title: str, filename: str, path: str) -> str:
    """Resolve display title with fallback chain: title -> filename -> path basename."""
    if title:
        return title
    if filename:
        return filename
    if path:
        return Path(path).name
    return "Unknown"


def _result_doc_id(result: Any, doc: Any) -> str:
    """Return the stable result row id when the search result exposes one."""
    doc_id = _get_field(result, "doc_id", "")
    if doc_id:
        return str(doc_id)
    doc_id = _get_field(doc, "_doc_id", "") or _get_field(doc, "doc_id", "")
    return str(doc_id) if doc_id else ""


def _process_semantic_results(raw_results: Any) -> list[dict[str, Any]]:
    """
    Process raw semantic search results into display-ready format.

    Args:
        raw_results: Raw results from the semantic search pipeline.

    Returns:
        List of result dicts with path, filename, snippet, and score fields.
    """
    if not raw_results:
        return []

    if isinstance(raw_results, dict):
        raw_results = [raw_results]

    results: list[dict[str, Any]] = []

    for result in raw_results:
        doc = result.document if hasattr(result, "document") else result

        doc_id = _result_doc_id(result, doc)
        path = (
            _get_field(doc, "path", "")
            or _get_field(doc, "source", "")
            or _get_field(doc, "id", "")
            or "Unknown"
        )
        snippet_text = (
            _get_field(doc, "content", "")
            or _get_field(doc, "shingle", "")
            or _get_field(doc, "shingle_text", "")
        )
        title = _get_field(doc, "title", "")
        filename = _get_field(doc, "filename", "")

        # Calculate score from distance
        if hasattr(result, "score"):
            score = result.score
        elif hasattr(result, "_distance"):
            score = 1 - result._distance
        elif isinstance(result, dict):
            score = 1 - result.get("_distance", 0)
        else:
            score = 0

        results.append(
            {
                "path": path,
                "lookup_path": doc_id or path,
                "filename": _resolve_title(title, filename, path),
                "snippet": _create_snippet(snippet_text),
                # Some vector backends (e.g. model2vec) don't return a usable similarity
                # score and report 0.0 for every hit. Rendering "Score: 0.0000" on every
                # result is misleading, so only surface a score when it's meaningful.
                "score": f"{score:.4f}" if score and score > 0 else "",
            }
        )

    return results


def _chat_citations(state: AppState, message: str) -> list[dict[str, Any]]:
    """Return display-ready source chunks for an Ask response."""
    if not state.search_pipeline:
        return []

    raw_results = state.search_pipeline(message)
    return _process_semantic_results(raw_results)[
        : _effective_models(state)["rag_result_limit"]
    ]


def _strip_answer_source_paths(answer: str) -> str:
    """Replace absolute paths in the answer's trailing Sources list with basenames.

    The RAG pipeline appends a "Sources:" section listing the retrieved files
    by absolute path. Search pages hide filesystem paths unless the server was
    started with --show-source-paths, so the Ask answer must do the same.
    """
    marker = "\n\nSources:\n"
    index = answer.rfind(marker)
    if index == -1:
        return answer

    head = answer[: index + len(marker)]
    lines = []
    for line in answer[index + len(marker) :].splitlines():
        if line.startswith("- "):
            lines.append("- " + os.path.basename(line[2:].strip()))
        else:
            lines.append(line)
    return head + "\n".join(lines)


def _answered_by(state: AppState) -> str:
    """Describe the chat provider/model that generates Ask answers."""
    models = _effective_models(state)
    if models["chat_source"] == "eliza":
        return (
            "eliza (built-in scripted responder for smoke tests — "
            "it does not use your documents)"
        )
    return f"{models['chat_source']} / {models['chat_model']}"


def _process_keyword_results(raw_results: list[Any]) -> list[dict[str, Any]]:
    """
    Process raw keyword search results into display-ready format.

    Args:
        raw_results: Raw results from the keyword search pipeline.

    Returns:
        List of result dicts with path, filename, snippet, and score fields.
    """
    results: list[dict[str, Any]] = []

    for result in raw_results:
        doc_id = _get_field(result, "doc_id", "")
        score = _get_field(result, "score", 0)
        document = _get_field(result, "document", None)
        if document is None:
            document = result

        content = _get_field(document, "content", "") or _get_field(
            result, "content", ""
        )
        path = (
            _get_field(document, "path", "")
            or _get_field(result, "path", "")
            or doc_id
            or "Unknown"
        )
        title = _get_field(document, "title", "")
        filename = _get_field(document, "filename", "") or _get_field(
            result, "filename", ""
        )

        results.append(
            {
                "path": path,
                "lookup_path": doc_id or path,
                "filename": _resolve_title(title, filename, path),
                "snippet": _create_snippet(content),
                "score": f"{score:.4f}",
            }
        )

    return results


def _extract_document_record(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a LanceDB docs-row into a dict-like document."""
    raw_doc = row.get("document", {})
    if isinstance(raw_doc, str):
        try:
            return json.loads(raw_doc)
        except json.JSONDecodeError:
            return {"content": raw_doc}
    if isinstance(raw_doc, dict):
        return raw_doc
    return {"content": str(raw_doc)}


def _load_docs_rows(vault_path: str) -> list[dict[str, Any]]:
    """Load rows from the TalkPipe docs table from common vault locations."""
    ensure_supported_vault_layout(vault_path)
    vectordb_path = get_vector_db_path(vault_path)
    doc_store = LanceDBDocumentStore(
        path=vectordb_path,
        table_name=DEFAULT_VECTOR_TABLE_NAME,
    )
    table, _ = doc_store._get_table()
    if hasattr(table, "to_arrow"):
        return table.to_arrow().to_pylist()
    if hasattr(table, "to_pandas"):
        return table.to_pandas().to_dict(orient="records")
    raise RuntimeError(
        "Unsupported LanceDB table reader: expected to_arrow() or to_pandas()."
    )


def _normalize_snippet_prefix(snippet: str) -> str:
    """Normalize UI snippet text for matching against stored chunk content."""
    normalized = " ".join(snippet.split())
    if normalized.endswith("..."):
        normalized = normalized[:-3].rstrip()
    return normalized


def _document_lookup_keys(row: dict[str, Any], doc: dict[str, Any]) -> set[str]:
    """Return non-empty identifiers that may be used to look up a document."""
    keys = {
        str(doc.get("path") or ""),
        str(doc.get("source") or ""),
        str(doc.get("id") or ""),
        str(row.get("id") or ""),
    }
    return {key for key in keys if key}


def _indexed_source_paths(vault_path: str) -> set[str]:
    """Return source file paths referenced by the current vault index."""
    rows = _load_docs_rows(vault_path)
    source_paths: set[str] = set()
    for row in rows:
        doc = _extract_document_record(row)
        for key in ("path", "source", "id"):
            value = str(doc.get(key) or "")
            if value:
                source_paths.add(value)
    return source_paths


def _get_chunk_text_for_path_and_snippet(
    vault_path: str, path: str, snippet: str
) -> str:
    """Return full text for a selected chunk identified by path/id and snippet."""
    rows = _load_docs_rows(vault_path)
    snippet_prefix = _normalize_snippet_prefix(snippet)
    content_by_snippet: list[str] = []
    for row in rows:
        doc = _extract_document_record(row)
        content = str(doc.get("content", "")).strip()
        if not content:
            continue
        candidate_prefix = " ".join(content.split())
        if snippet_prefix and snippet_prefix in candidate_prefix:
            content_by_snippet.append(content)
        if path not in _document_lookup_keys(row, doc):
            continue
        if snippet_prefix and snippet_prefix not in candidate_prefix:
            continue
        return content
    if snippet_prefix and content_by_snippet:
        return content_by_snippet[0]
    return ""


def _vault_selected(state: AppState) -> bool:
    """Return True when a vault is currently selected."""
    return bool(state.vault_path)


def _require_vault(state: AppState) -> RedirectResponse | None:
    """Redirect to the vault manager when no vault is selected."""
    if _vault_selected(state):
        return None
    return _redirect_with_message(
        "/vaults", error="Choose or create a vault to get started."
    )


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
    if path.strip():
        base = Path(path).expanduser()
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
        base = roots[0] if roots else Path.home()
    try:
        base = base.resolve()
        if not access_control.is_allowed(base, roots):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Browsing on this server is limited to "
                    f"{access_control.describe(roots)}."
                },
            )
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
    except OSError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    if base in roots:
        # At an allowed root: "up" goes to the virtual root listing when
        # there are several roots, and nowhere when there is only one.
        parent = "" if len(roots) > 1 else None
    else:
        parent = str(base.parent) if base.parent != base else None
    if roots:
        home = str(roots[0]) if len(roots) == 1 else ""
    else:
        home = str(Path.home())
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
) -> HTMLResponse:
    """Render the home page with navigation to search and chat."""
    redirect = _require_vault(state)
    if redirect:
        return redirect
    # Refresh document counts on home page load
    _update_document_counts(state.vault_path)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=_template_context(
            request, state, flash_message=message, flash_error=error
        ),
    )


@app.get("/vaults", response_class=HTMLResponse)
async def vaults_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Render the vault manager for creating or choosing a vault."""
    # Suggest an example path the server will actually accept: with a vault
    # root configured (e.g. in the container, where ~ is ephemeral and outside
    # the fence), point under the root instead of the home directory.
    root = access_control.vault_root()
    vault_example = str(root / "my-vault") if root else "~/my-vault"
    return templates.TemplateResponse(
        request=request,
        name="vaults.html",
        context=_template_context(
            request,
            state,
            recent_vaults=user_settings.get_recent_vaults(),
            vault_example=vault_example,
            flash_message=message,
            flash_error=error,
        ),
    )


@app.post("/vaults/open", response_class=HTMLResponse)
async def open_vault(
    new_vault_path: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Open an existing vault, or create a new one when the path doesn't exist.

    Expects form data with:
        - new_vault_path: str - Directory for the vault (created if missing)
    """
    raw_path = new_vault_path.strip()
    if not raw_path:
        return _redirect_with_message("/vaults", error="Enter a vault path.")

    vault_path = Path(raw_path).expanduser()
    root = access_control.vault_root()
    if root is not None and not access_control.is_allowed(vault_path, [root]):
        return _redirect_with_message(
            "/vaults",
            error=f"Vaults on this server must live under {root}.",
        )
    if vault_path.is_file():
        return _redirect_with_message(
            "/vaults",
            error=f"{vault_path} is a file. A vault must be a directory.",
        )

    created = not vault_path.is_dir()

    # A folder of ordinary files (e.g. the user's documents folder) is easy to
    # confuse with a vault folder. Detect that case before index scaffolding is
    # written into it, so the user is told what is about to happen.
    non_empty_non_vault = False
    if not created:
        vault_markers = ("docs.lance", "vault_metadata.json", "fulltext_vault")
        looks_like_vault = any(
            (vault_path / marker).exists() for marker in vault_markers
        )
        non_empty_non_vault = not looks_like_vault and any(vault_path.iterdir())

    try:
        vault_path.mkdir(parents=True, exist_ok=True)
        init_pipelines(str(vault_path))
    except (OSError, ValueError) as exc:
        return _redirect_with_message("/vaults", error=str(exc))

    user_settings.remember_vault(str(vault_path))
    if created:
        return _redirect_with_message(
            "/documents",
            message=(
                f"Created new vault at {vault_path}. "
                "Add documents to make it searchable."
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
                "folder from this page instead."
            ),
        )
    return _redirect_with_message("/", message=f"Opened vault at {vault_path}.")


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


@app.post("/vaults/delete", response_class=HTMLResponse)
async def delete_vault(
    vault_path: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> HTMLResponse:
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
        return _redirect_with_message("/vaults", error="No vault specified.")
    if confirm != "delete":
        return _redirect_with_message("/vaults", error="Deletion was not confirmed.")

    resolved = str(Path(raw_path).expanduser())

    # Only vaults the user already knows about are deletable — never an
    # arbitrary path supplied to this endpoint.
    if resolved not in user_settings.get_recent_vaults():
        return _redirect_with_message(
            "/vaults", error="That vault is not in the recent list."
        )

    # Refuse to delete the vault currently open in the app.
    if state.vault_path and os.path.realpath(state.vault_path) == os.path.realpath(
        resolved
    ):
        return _redirect_with_message(
            "/vaults",
            error="Cannot delete the vault that is currently open. "
            "Open a different vault first.",
        )

    real_path = Path(os.path.realpath(resolved))
    if _is_dangerous_delete_target(real_path):
        return _redirect_with_message(
            "/vaults",
            error=f"Refusing to delete {resolved}: path is too broad to be a vault.",
        )

    # Vaults outside the configured root (e.g. remembered before the
    # restriction existed) can be forgotten, but their files are never
    # touched.
    root = access_control.vault_root()
    if root is not None and not access_control.is_allowed(resolved, [root]):
        user_settings.forget_vault(resolved)
        return _redirect_with_message(
            "/vaults",
            message=(
                f"Removed {resolved} from the list. It is outside {root}, "
                "so its files were left untouched."
            ),
        )

    existed = os.path.isdir(resolved)
    try:
        if existed:
            shutil.rmtree(resolved)
    except OSError as exc:
        return _redirect_with_message(
            "/vaults", error=f"Failed to delete {resolved}: {exc}"
        )

    user_settings.forget_vault(resolved)
    if existed:
        message = f"Deleted vault {resolved} and removed it from the list."
    else:
        message = (
            f"Removed {resolved} from the list "
            "(its folder was already gone from disk)."
        )
    return _redirect_with_message("/vaults", message=message)


@app.get("/documents", response_class=HTMLResponse)
async def documents_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Render the page for indexing documents into the current vault."""
    redirect = _require_vault(state)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="documents.html",
        context=_template_context(
            request,
            state,
            models=_effective_models(state),
            flash_message=message,
            flash_error=error,
        ),
    )


def _resolve_source_pattern(raw_source: str) -> str:
    """Turn a folder path into a recursive glob; leave explicit globs alone."""
    source = os.path.expanduser(raw_source.strip())
    if os.path.isdir(source):
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
        retrieval_template = get_retrieval_template()
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
        return {
            "running": _index_job.running,
            "source": _index_job.source,
            "vault_path": _index_job.vault_path,
            "embedding": _index_job.embedding,
            "total_files": _index_job.total_files,
            "files_done": _index_job.files_done,
            "current_file": _index_job.current_file,
            "chunks": _index_job.chunks,
            "message": _index_job.message,
            "error": _index_job.error,
        }


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
    """Thread target: run one indexing job, publishing progress as it goes."""

    def report(chunks: int, files_done: int, current: str) -> None:
        with _index_job_lock:
            _index_job.chunks = chunks
            _index_job.files_done = files_done
            _index_job.current_file = Path(current).name if current else ""

    try:
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
    total_files = sum(
        1 for p in globlib.iglob(pattern, recursive=True) if os.path.isfile(p)
    )
    with _index_job_lock:
        if _index_job.running:
            return False
        _index_job.running = True
        _index_job.source = source
        _index_job.vault_path = vault_path
        _index_job.embedding = f"{embedding_source}/{embedding_model}"
        _index_job.total_files = total_files
        _index_job.files_done = 0
        _index_job.current_file = ""
        _index_job.chunks = 0
        _index_job.message = ""
        _index_job.error = None

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


@app.get("/api/index-status")
async def index_status() -> JSONResponse:
    """Report the state of the background document-indexing job."""
    return JSONResponse(content=_index_job_snapshot())


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
        return {
            "running": _fulltext_index_job.running,
            "vault_path": _fulltext_index_job.vault_path,
            "total_docs": _fulltext_index_job.total_docs,
            "docs_done": _fulltext_index_job.docs_done,
            "message": _fulltext_index_job.message,
            "error": _fulltext_index_job.error,
        }


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
    with _fulltext_index_job_lock:
        if _fulltext_index_job.running:
            return False
        _fulltext_index_job.running = True
        _fulltext_index_job.vault_path = vault_path
        _fulltext_index_job.total_docs = 0
        _fulltext_index_job.docs_done = 0
        _fulltext_index_job.message = ""
        _fulltext_index_job.error = None

    thread = threading.Thread(
        target=_run_fulltext_index_job,
        args=(vault_path,),
        daemon=True,
    )
    thread.start()
    return True


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


def _collect_config_status(
    state: AppState, probe: bool = True, allow_download: bool = False
) -> dict[str, Any]:
    """Build the config-status report for the effective model selection."""
    vault_selected = bool(state.vault_path)
    vault_embedding = (
        vault_metadata.load_embedding_config(state.vault_path)
        if vault_selected
        else None
    )
    return diagnostics.collect_config_status(
        _effective_models(state),
        vault_selected=vault_selected,
        vault_embedding=vault_embedding,
        vault_indexed=vault_selected and _vault_has_documents(state.vault_path),
        probe=probe,
        allow_download=allow_download,
    )


def _vault_has_documents(vault_path: str) -> bool:
    """True when the vault's docs table exists and holds at least one chunk.

    Any failure to read the table (no table yet, unreadable vault) counts as
    "no documents": for the embedding↔index diagnostic that is the state where
    no recorded config should be expected.
    """
    try:
        doc_store = LanceDBDocumentStore(
            path=get_vector_db_path(vault_path),
            table_name=DEFAULT_VECTOR_TABLE_NAME,
        )
        return doc_store.count() > 0
    except Exception:
        return False


@app.post("/documents/index", response_class=HTMLResponse)
async def index_documents(
    source_path: Annotated[str, Form()] = "",
    overwrite: Annotated[bool, Form()] = False,
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Index documents from a folder or glob pattern into the current vault.

    Expects form data with:
        - source_path: str - Folder path or glob pattern of documents to index
        - overwrite: bool - Replace the existing index instead of adding to it
    """
    redirect = _require_vault(state)
    if redirect:
        return redirect

    if not source_path.strip():
        return _redirect_with_message(
            "/documents", error="Enter a folder or glob pattern to index."
        )

    pattern = _resolve_source_pattern(source_path)
    doc_roots = access_control.document_roots()
    if doc_roots and not access_control.is_allowed(_nonglob_prefix(pattern), doc_roots):
        return _redirect_with_message(
            "/documents",
            error="Indexing on this server is limited to documents under "
            f"{access_control.describe(doc_roots)}.",
        )
    if not globlib.glob(pattern, recursive=True):
        return _redirect_with_message(
            "/documents",
            error=(
                f"'{source_path.strip()}' matched no files. Check the folder "
                "path or glob pattern."
            ),
        )

    models = _effective_models(state)
    started = start_index_job(
        vault_path=state.vault_path,
        source=source_path.strip(),
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
            "Indexing started in the background — progress is shown below. "
            f"Embedding with {models['embedding_source']}/{models['embedding_model']}."
        ),
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Render the model configuration page."""
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=_template_context(
            request,
            state,
            models=_effective_models(state),
            embedding_sources=getEmbeddingSources(),
            chat_sources=getPromptSources(),
            credentials=credentials.describe(),
            credentials_store=str(credentials.store_path()),
            flash_message=message,
            flash_error=error,
        ),
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
) -> HTMLResponse:
    """Save vault-scoped provider credentials and connection settings.

    Non-secret fields (base/server URLs) are set from the form directly — a
    blank value clears them. Secret fields (API keys) are never echoed back, so
    a blank value keeps the saved key and the matching "clear" checkbox removes
    it. Saved values are applied to the process environment immediately.
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

    credentials.set_values(changes)
    if _vault_selected(state):
        _refresh_pipelines(force=True)
    return _redirect_with_message(
        "/settings", message="Connection settings saved for this app."
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
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Save model configuration chosen in the web interface.

    Expects form data with:
        - embedding_source / embedding_model: provider and model for embeddings
        - chat_source / chat_model: provider and model for chat/completion
    """
    previous_embedding = (
        _effective_models(state)["embedding_source"],
        _effective_models(state)["embedding_model"],
    )

    try:
        chunk_size_value = _parse_int_setting(chunk_size, "Chunk size", 1)
        shingle_size_value = _parse_int_setting(shingle_size, "Shingle size", 1)
        shingle_overlap_value = _parse_int_setting(
            shingle_overlap, "Shingle overlap", 0
        )
        rag_result_limit_value = _parse_int_setting(
            rag_result_limit, "Ask result count", 1
        )
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
    if embedding_changed:
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
) -> HTMLResponse:
    """Refresh pipelines and document counts, then redirect to home."""
    _refresh_pipelines(force=True)
    _update_document_counts(state.vault_path)

    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/"

    return RedirectResponse(url=return_to, status_code=303)


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Render the semantic search interface page."""
    redirect = _require_vault(state)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context=_template_context(
            request,
            state,
            query="",
            results=None,
        ),
    )


@app.post("/search", response_class=HTMLResponse)
async def search_results(
    request: Request,
    query: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """
    Process a semantic search query and return results.

    Expects form data with:
        - query: str - The search query string

    Returns HTML page with search results containing path, filename, snippet, and score.
    """
    results: list[dict[str, Any]] = []
    error: str | None = None

    if query.strip() and state.search_pipeline:
        try:
            # Refresh pipelines to ensure fresh database connections
            _refresh_pipelines()
            # Update document counts to reflect latest state
            _update_document_counts(state.vault_path)
            # Perform search with refreshed pipeline
            raw_results = state.search_pipeline(query)
            results = _process_semantic_results(raw_results)
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
        ),
    )


@app.get("/keyword-search", response_class=HTMLResponse)
async def keyword_search_page(
    request: Request,
    created: str | None = None,
    error: str | None = None,
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Render the keyword search interface page."""
    redirect = _require_vault(state)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="keyword_search.html",
        context=_template_context(
            request,
            state,
            query="",
            results=None,
            flash_message=created,
            flash_error=error,
        ),
    )


@app.post("/keyword-search", response_class=HTMLResponse)
async def keyword_search_results(
    request: Request,
    query: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """
    Process a keyword search query and return results.

    Expects form data with:
        - query: str - The keyword search query (Whoosh query syntax)

    Returns HTML page with search results containing path, filename, snippet, and score.
    """
    results: list[dict[str, Any]] = []
    error: str | None = None

    # Refresh before checking availability so the UI responds to newly added indexes.
    _refresh_pipelines()
    _update_document_counts(state.vault_path)

    if query.strip() and not state.keyword_search_enabled:
        error = "Keyword search is disabled because this vault has no Whoosh index."
    elif query.strip() and state.keyword_search_pipeline:
        try:
            # Perform search with refreshed pipeline
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
        ),
    )


@app.post("/keyword-search/create-index", response_class=HTMLResponse)
async def create_keyword_index(
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """Start a background rebuild of the Whoosh full-text index.

    The build runs in a worker thread that publishes progress via
    ``/api/fulltext-index-status``; the Keyword Search page polls it and shows a
    live progress bar, then reloads with the outcome.
    """
    if not state.vault_path:
        return RedirectResponse(
            url="/keyword-search?error=Vault%20path%20is%20not%20configured.",
            status_code=303,
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
        full_text = _get_chunk_text_for_path_and_snippet(
            state.vault_path, path, snippet
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to load chunk content: {exc}"},
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

    source_path = Path(path)
    try:
        allowed_paths = _indexed_source_paths(state.vault_path)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to verify source file path: {exc}"},
        )

    if str(source_path) not in allowed_paths:
        return JSONResponse(
            status_code=404,
            content={"error": "Source file is not referenced by this vault."},
        )

    if not source_path.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": "Source file was not found."},
        )

    return FileResponse(source_path, filename=source_path.name)


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Render the Ask interface page."""
    redirect = _require_vault(state)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context=_template_context(
            request,
            state,
            messages=[],
            citations=[],
        ),
    )


@app.post("/chat", response_class=HTMLResponse)
async def chat_response(
    request: Request,
    message: Annotated[str, Form()] = "",
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """
    Process a question and return AI-generated response.

    Expects form data with:
        - message: str - The user's question

    Returns HTML page with the question and AI-generated answer.
    """
    messages: list[dict[str, str]] = []
    citations: list[dict[str, Any]] = []
    error: str | None = None

    if message.strip() and state.chat_pipeline:
        messages.append({"role": "user", "content": message})

        try:
            # Refresh pipelines to ensure fresh database connections
            _refresh_pipelines()
            # Update document counts to reflect latest state
            _update_document_counts(state.vault_path)
            citations = _chat_citations(state, message)
            # Perform chat with refreshed pipeline
            response = state.chat_pipeline(message)
            if not state.show_source_paths:
                # The RAG answer text ends with a "Sources:" list of absolute
                # paths; hide them unless --show-source-paths was given, to
                # match the search pages.
                response = _strip_answer_source_paths(response)
            messages.append({"role": "assistant", "content": response})
        except Exception as e:
            error = str(e)
            lowered = error.lower()
            if "ollama" in lowered and ("connect" in lowered or "refused" in lowered):
                error += (
                    " Tip: you can also set the Ollama server URL in this app "
                    "under Settings > Connections & credentials."
                )
            elif ("openai" in lowered or "anthropic" in lowered) and (
                "api key" in lowered or "api_key" in lowered or "credential" in lowered
            ):
                error += (
                    " Tip: you can also enter the API key in this app "
                    "under Settings > Connections & credentials."
                )

    if not state.show_source_paths:
        # The citations list is embedded in the page as JSON; keep absolute
        # filesystem paths out of it unless --show-source-paths was given.
        # The UI resolves chunk content through lookup_path, not path.
        citations = [
            {key: value for key, value in citation.items() if key != "path"}
            for citation in citations
        ]

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context=_template_context(
            request,
            state,
            messages=messages,
            citations=citations,
            error=error,
            answered_by=_answered_by(state),
        ),
    )


def run_app(
    vault_path: str = "",
    host: str = "127.0.0.1",
    port: int = 8000,
    show_source_paths: bool = False,
    open_browser: bool = True,
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
    """
    problems = access_control.startup_errors()
    if problems:
        raise ValueError(" ".join(problems))
    root = access_control.vault_root()
    if (
        vault_path
        and root is not None
        and not access_control.is_allowed(vault_path, [root])
    ):
        raise ValueError(
            f"Vault path {vault_path} is outside "
            f"{access_control.VAULT_ROOT_ENV} ({root})."
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
    if vault_path:
        # Start degraded rather than not at all: an embedder that cannot load
        # (e.g. the model is not cached and Hugging Face is unreachable) should
        # not keep the whole server — and its diagnostics — from coming up.
        # ValueError still propagates: it marks deliberate preflight failures
        # (unsupported vault layout) whose message the caller shows the user.
        try:
            init_pipelines(vault_path)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - degrade to no-vault startup
            print(
                f"Warning: could not open vault at {vault_path}: {exc}\n"
                "Starting without a vault - check Settings -> Configuration "
                "status, then reopen the vault from the Vaults page.",
                file=sys.stderr,
            )
            _state.vault_path = ""
            _state.search_pipeline = None
            _state.chat_pipeline = None
            _state.keyword_search_pipeline = None
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
        except Exception as exc:  # noqa: BLE001 - headless / no browser available
            logger.debug("Could not open a browser for %s: %s", url, exc)

    threading.Thread(target=_wait_and_open, daemon=True).start()


def main() -> None:
    """CLI entry point for vault query web application."""
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
