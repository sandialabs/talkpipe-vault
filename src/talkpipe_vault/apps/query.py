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
import os
import shutil
import threading
import urllib.parse
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
    MakeVectorDatabaseSegment,
    ProcessDocumentsSegment,
)
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshFullTextIndex
from talkpipe.util.config import configure_logger

from talkpipe_vault.apps import user_settings
from talkpipe_vault.pipelines.config import (
    DEFAULT_VECTOR_TABLE_NAME,
    ensure_supported_vault_layout,
    get_chat_model,
    get_chat_source,
    get_embedding_model,
    get_embedding_source,
    get_vector_db_path,
    get_whoosh_index_path,
)
from talkpipe_vault.pipelines.searching_and_prompting import (
    VaultChat,
    VaultSearch,
    VaultTextSearch,
)

configure_logger("root:ERROR")

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


def load_saved_model_overrides() -> None:
    """Apply model overrides persisted by the settings page to app state."""
    overrides = user_settings.get_model_overrides()
    _state.embedding_model = overrides.get("embedding_model")
    _state.embedding_source = overrides.get("embedding_source")
    _state.chat_model = overrides.get("chat_model")
    _state.chat_source = overrides.get("chat_source")
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


def _build_whoosh_index(vault_path: str, documents: list[dict[str, str]]) -> None:
    """Rebuild the vault's Whoosh full-text index from normalized documents.

    Expects documents as dicts with doc_id, content, path, and filename keys.
    The existing index is replaced. Documents keep their LanceDB row ids as
    Whoosh doc_ids so search results can be resolved back to stored chunks.
    (talkpipe's indexWhoosh segment reserves the doc_id schema field, so the
    index is built through WhooshFullTextIndex to control ids directly.)
    """
    whoosh_index_path = get_whoosh_index_path(vault_path)
    if os.path.isdir(whoosh_index_path):
        shutil.rmtree(whoosh_index_path)

    with WhooshFullTextIndex(whoosh_index_path, fields=WHOOSH_INDEX_FIELDS) as ix:
        for document in documents:
            ix.add_document(
                {field: document.get(field, "") for field in WHOOSH_INDEX_FIELDS},
                doc_id=document.get("doc_id") or None,
            )


def _print_whoosh_index_documents(documents: list[dict[str, str]]) -> None:
    """Print the full document text sent to the Whoosh index."""
    print(
        f"Indexing {len(documents)} document(s) into the Whoosh full-text index.",
        flush=True,
    )
    for index, document in enumerate(documents, start=1):
        print(
            f"\n--- Whoosh document {index}/{len(documents)} ---\n"
            f"doc_id: {document.get('doc_id', '')}\n"
            f"path: {document.get('path', '')}\n"
            f"filename: {document.get('filename', '')}\n"
            "content:",
            flush=True,
        )
        print(document.get("content", ""), flush=True)
    print("--- End Whoosh document dump ---", flush=True)


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
    _refresh_pipelines()

    # Get document counts from storage locations
    _update_document_counts(vault_path)


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
                "score": f"{score:.4f}",
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

    Returns the resolved path, its parent (null at the filesystem root), and
    the visible (non-hidden) subdirectories. Falls back to the user's home
    directory when no path is given; unreadable or nonexistent paths return
    a 400 with an error message the dialog can display.
    """
    base = Path(path).expanduser() if path.strip() else Path.home()
    try:
        base = base.resolve()
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

    parent = str(base.parent) if base.parent != base else None
    return JSONResponse(
        content={
            "path": str(base),
            "parent": parent,
            "home": str(Path.home()),
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
    return templates.TemplateResponse(
        request=request,
        name="vaults.html",
        context=_template_context(
            request,
            state,
            recent_vaults=user_settings.get_recent_vaults(),
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
    if vault_path.is_file():
        return _redirect_with_message(
            "/vaults",
            error=f"{vault_path} is a file. A vault must be a directory.",
        )

    created = not vault_path.is_dir()
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
    return _redirect_with_message("/", message=f"Opened vault at {vault_path}.")


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
) -> int:
    """Index documents matching a glob into the vault's docs table.

    Uses TalkPipe's document pipeline (the same one behind the
    makevectordatabase CLI) so the resulting table matches what the
    search/chat pipelines expect. Returns the number of indexed chunks.
    The optional progress callback receives (chunks_done, files_done,
    current_source_path) as chunks flow through the pipeline.
    """
    ensure_supported_vault_layout(vault_path)
    pipeline = ProcessDocumentsSegment(
        chunk_size=chunk_size,
        shingle_size=shingle_size,
        overlap=shingle_overlap,
    ) | MakeVectorDatabaseSegment(
        embedding_field="shingle_text",
        embedding_model=embedding_model,
        embedding_source=embedding_source,
        path=get_vector_db_path(vault_path),
        table_name=DEFAULT_VECTOR_TABLE_NAME,
        doc_id_field=None,
        overwrite=overwrite,
        batch_size=25,
        fail_on_error=False,
    )
    chunk_count = 0
    seen_sources: set[str] = set()
    for item in pipeline.transform([source_pattern]):
        chunk_count += 1
        if progress is not None:
            source = str(_get_field(item, "source", "") or "")
            if source:
                seen_sources.add(source)
            progress(chunk_count, len(seen_sources), source)
    return chunk_count


@dataclass
class IndexJob:
    """Progress of the (single) background document-indexing job."""

    running: bool = False
    source: str = ""
    vault_path: str = ""
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
        chunk_count = index_documents_into_vault(
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
            flash_message=message,
            flash_error=error,
        ),
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
    """Create a Whoosh full-text index using existing LanceDB docs-table records."""
    if not state.vault_path:
        return RedirectResponse(
            url="/keyword-search?error=Vault%20path%20is%20not%20configured.",
            status_code=303,
        )

    try:
        documents = _iter_lancedb_docs_for_whoosh(state.vault_path)
        _print_whoosh_index_documents(documents)
        _build_whoosh_index(state.vault_path, documents)
        _refresh_pipelines(force=True)
        _update_document_counts(state.vault_path)
    except Exception as exc:
        return _redirect_with_message(
            "/keyword-search", error=f"Failed to create index: {exc}"
        )

    return _redirect_with_message(
        "/keyword-search",
        created=f"Full-text index created with {len(documents)} document(s).",
    )


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
            messages.append({"role": "assistant", "content": response})
        except Exception as e:
            error = str(e)

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context=_template_context(
            request,
            state,
            messages=messages,
            citations=citations,
            error=error,
        ),
    )


def run_app(
    vault_path: str = "",
    host: str = "127.0.0.1",
    port: int = 8000,
    show_source_paths: bool = False,
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
    """
    _state.show_source_paths = show_source_paths
    load_saved_model_overrides()
    if vault_path:
        init_pipelines(vault_path)
        user_settings.remember_vault(vault_path)
    uvicorn.run(app, host=host, port=port)


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

    args = parser.parse_args()
    run_app(args.vault_path, args.host, args.port, args.show_source_paths)


if __name__ == "__main__":
    main()
