"""
Web application for querying and chatting with vault contents.

Provides three interaction modes via web interface:
- Semantic Search: Vector similarity search returning ranked results
- Keyword Search: Full-text search using a Whoosh index
- Ask: Single-turn RAG-based Q&A interface
"""

import argparse
import json
import os
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
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshFullTextIndex, indexWhoosh
from talkpipe.util.config import configure_logger

from talkpipe_vault.pipelines.config import (
    DEFAULT_VECTOR_TABLE_NAME,
    get_vault_paths,
    get_vector_db_path,
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


def _keyword_search_enabled(vault_path: str) -> bool:
    """Return True when the vault has a readable Whoosh full-text index."""
    if not vault_path:
        return False

    _, whoosh_index_path = get_vault_paths(vault_path)

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
    candidate_paths = []
    vectordb_path = get_vector_db_path(vault_path)
    candidate_paths.append(vectordb_path)
    vector_subdir_path, _ = get_vault_paths(vault_path)
    if vector_subdir_path not in candidate_paths:
        candidate_paths.append(vector_subdir_path)

    candidate_tables = [DEFAULT_VECTOR_TABLE_NAME]

    last_error = None
    rows: list[dict[str, Any]] = []
    selected_table: str | None = None
    for candidate_path in candidate_paths:
        for candidate_table in candidate_tables:
            try:
                doc_store = LanceDBDocumentStore(
                    path=candidate_path,
                    table_name=candidate_table,
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
                selected_table = candidate_table
                break
            except Exception as exc:
                last_error = exc
                continue
        if selected_table is not None:
            break
    else:
        raise RuntimeError(
            "Could not read LanceDB docs table at expected paths."
        ) from last_error

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

    _state.search_pipeline = VaultSearch(vault_path=vault_path).as_function(
        single_in=True, single_out=True
    )
    _state.chat_pipeline = VaultChat(vault_path=vault_path).as_function(
        single_in=True, single_out=True
    )
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
    candidate_paths = []
    vectordb_path = get_vector_db_path(vault_path)
    candidate_paths.append(vectordb_path)
    vector_subdir_path, _ = get_vault_paths(vault_path)
    if vector_subdir_path not in candidate_paths:
        candidate_paths.append(vector_subdir_path)

    last_error = None
    for candidate_path in candidate_paths:
        try:
            doc_store = LanceDBDocumentStore(
                path=candidate_path,
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
        except Exception as exc:
            last_error = exc
            continue

    raise RuntimeError("Could not read LanceDB docs table at expected paths.") from last_error


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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
    """Render the home page with navigation to search and chat."""
    # Refresh document counts on home page load
    _update_document_counts(state.vault_path)
    return templates.TemplateResponse(
        "home.html",
        _template_context(request, state),
    )


@app.post("/refresh", response_class=HTMLResponse)
async def refresh(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Refresh pipelines and document counts, then redirect to home."""
    _refresh_pipelines(force=True)
    _update_document_counts(state.vault_path)

    return RedirectResponse(url="/", status_code=303)


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Render the semantic search interface page."""
    return templates.TemplateResponse(
        "search.html",
        _template_context(
            request,
            state,
            query="",
            results=None,
        ),
    )


@app.post("/search", response_class=HTMLResponse)
async def search_results(
    request: Request,
    query: Annotated[str, Form()],
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
        "search.html",
        _template_context(
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
    return templates.TemplateResponse(
        "keyword_search.html",
        _template_context(
            request,
            state,
            query="",
            results=None,
            create_status=created,
            create_error=error,
        ),
    )


@app.post("/keyword-search", response_class=HTMLResponse)
async def keyword_search_results(
    request: Request,
    query: Annotated[str, Form()],
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
        error = (
            "Keyword search is disabled because this vault has no Whoosh index."
        )
    elif query.strip() and state.keyword_search_pipeline:
        try:
            # Perform search with refreshed pipeline
            raw_results = list(state.keyword_search_pipeline(query))
            results = _process_keyword_results(raw_results)
        except Exception as e:
            error = str(e)

    return templates.TemplateResponse(
        "keyword_search.html",
        _template_context(
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
        _, whoosh_index_path = get_vault_paths(state.vault_path)

        pipeline = indexWhoosh(
            index_path=whoosh_index_path,
            field_list="content:content,path:path,filename:filename,doc_id:doc_id",
            overwrite=True,
            commit_seconds=0,
        )
        list(pipeline(documents))
        _refresh_pipelines(force=True)
        _update_document_counts(state.vault_path)
    except Exception as exc:
        return RedirectResponse(
            url=f"/keyword-search?error=Failed%20to%20create%20index%3A%20{str(exc)}",
            status_code=303,
        )

    return RedirectResponse(
        url="/keyword-search?created=Whoosh%20index%20created.",
        status_code=303,
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
    return templates.TemplateResponse(
        "chat.html",
        _template_context(
            request,
            state,
            messages=[],
        ),
    )


@app.post("/chat", response_class=HTMLResponse)
async def chat_response(
    request: Request,
    message: Annotated[str, Form()],
    state: AppState = Depends(get_state),
) -> HTMLResponse:
    """
    Process a question and return AI-generated response.

    Expects form data with:
        - message: str - The user's question

    Returns HTML page with the question and AI-generated answer.
    """
    messages: list[dict[str, str]] = []
    error: str | None = None

    if message.strip() and state.chat_pipeline:
        messages.append({"role": "user", "content": message})

        try:
            # Refresh pipelines to ensure fresh database connections
            _refresh_pipelines()
            # Update document counts to reflect latest state
            _update_document_counts(state.vault_path)
            # Perform chat with refreshed pipeline
            response = state.chat_pipeline(message)
            messages.append({"role": "assistant", "content": response})
        except Exception as e:
            error = str(e)

    return templates.TemplateResponse(
        "chat.html",
        _template_context(
            request,
            state,
            messages=messages,
            error=error,
        ),
    )


def run_app(
    vault_path: str,
    host: str = "127.0.0.1",
    port: int = 8000,
    show_source_paths: bool = False,
) -> None:
    """
    Start the web application server.

    Args:
        vault_path: Path to LanceDB created by makevectordatabase.
        host: Host to bind to (default: 127.0.0.1)
        port: Port to listen on (default: 8000)
        show_source_paths: If True, display and serve source file paths in search results.
    """
    _state.show_source_paths = show_source_paths
    init_pipelines(vault_path)
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    """CLI entry point for vault query web application."""
    parser = argparse.ArgumentParser(
        description="Web application for searching and chatting with your vault"
    )
    parser.add_argument(
        "vault_path",
        help=(
            "Path to LanceDB directory containing the TalkPipe docs table "
            "(e.g., output path from makevectordatabase)"
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
