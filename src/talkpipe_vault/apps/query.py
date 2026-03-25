"""
Web application for querying and chatting with vault contents.

Provides three interaction modes via web interface:
- Semantic Search: Vector similarity search returning ranked results
- Keyword Search: Full-text search using Whoosh index
- Ask: Single-turn RAG-based Q&A interface
"""
import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable

import uvicorn
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from talkpipe.util.config import configure_logger

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
    full_documents_count: int = 0
    shingled_chunks_count: int = 0
    fulltext_documents_count: int = 0
    last_refresh_time: float = 0.0


# Application state singleton
_state = AppState()

app = FastAPI(title="Talkpipe Vault", description="Search and chat with your vault")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_state() -> AppState:
    """Dependency that provides access to application state."""
    return _state


def init_pipelines(vault_path: str) -> None:
    """
    Initialize the search and chat pipelines.

    This function modifies the global application state to set up pipelines
    for semantic search, keyword search, and RAG-based chat.

    Args:
        vault_path: Base path for vault storage. Vector DB is located at
            vault_path/vector_vault, full-text index at vault_path/fulltext_vault.
    """
    import os
    from pathlib import Path
    
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
    _state.keyword_search_pipeline = VaultTextSearch(vault_path=vault_path).as_function(
        single_in=True, single_out=False
    )
    _state.last_refresh_time = current_time


def _update_document_counts(vault_path: str) -> None:
    """Update document counts from vault storage locations."""
    import os
    from pathlib import Path
    
    vectordb_path = os.path.join(vault_path, "vector_vault")
    whoosh_path = os.path.join(vault_path, "fulltext_vault")
    
    # Get counts from LanceDB tables
    try:
        from talkpipe.search.lancedb import LanceDBDocumentStore

        # Full documents count
        try:
            db = LanceDBDocumentStore(path=vectordb_path, table_name="full_documents")
            _state.full_documents_count = db.count()
        except Exception:
            _state.full_documents_count = 0
        
        # Shingled chunks count
        try:
            db = LanceDBDocumentStore(path=vectordb_path, table_name="shingled_chunks")
            _state.shingled_chunks_count = db.count()
        except Exception:
            _state.shingled_chunks_count = 0
    except Exception:
        _state.full_documents_count = 0
        _state.shingled_chunks_count = 0
    
    # Get count from Whoosh index
    try:
        from whoosh.index import open_dir
        ix = open_dir(whoosh_path)
        with ix.searcher() as searcher:
            _state.fulltext_documents_count = searcher.doc_count_all()
    except Exception:
        _state.fulltext_documents_count = 0


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


def _process_semantic_results(raw_results: Any) -> list[dict[str, Any]]:
    """
    Process raw semantic search results into display-ready format.

    Deduplicates results by path, keeping the best score for each document.

    Args:
        raw_results: Raw results from the semantic search pipeline.

    Returns:
        List of result dicts with path, filename, snippet, and score fields.
    """
    if not raw_results:
        return []

    if isinstance(raw_results, dict):
        raw_results = [raw_results]

    seen_paths: dict[str, dict[str, Any]] = {}

    for result in raw_results:
        doc = result.document if hasattr(result, "document") else result

        path = _get_field(doc, "path", "Unknown")
        shingle = _get_field(doc, "shingle", "")
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

        # Skip if we've seen this path with a better score
        if path in seen_paths and seen_paths[path]["score_val"] >= score:
            continue

        seen_paths[path] = {
            "path": path,
            "filename": _resolve_title(title, filename, path),
            "snippet": _create_snippet(shingle),
            "score": f"{score:.4f}",
            "score_val": score,
        }

    # Remove score_val (used only for comparison)
    return [
        {k: v for k, v in item.items() if k != "score_val"}
        for item in seen_paths.values()
    ]


def _process_keyword_results(raw_results: list[Any]) -> list[dict[str, Any]]:
    """
    Process raw keyword search results into display-ready format.

    Deduplicates results by path, keeping the best score for each document.

    Args:
        raw_results: Raw results from the keyword search pipeline.

    Returns:
        List of result dicts with path, filename, snippet, and score fields.
    """
    seen_paths: dict[str, dict[str, Any]] = {}

    for result in raw_results:
        doc_id = _get_field(result, "doc_id", "Unknown")
        score = _get_field(result, "score", 0)
        document = _get_field(result, "document", {})

        content = _get_field(document, "content", "")
        path = _get_field(document, "path", doc_id)
        title = _get_field(document, "title", "")
        filename = _get_field(document, "filename", "")

        # Skip if we've seen this path with a better score
        if path in seen_paths and seen_paths[path]["score_val"] >= score:
            continue

        seen_paths[path] = {
            "path": path,
            "filename": _resolve_title(title, filename, path),
            "snippet": _create_snippet(content),
            "score": f"{score:.4f}",
            "score_val": score,
        }

    # Remove score_val (used only for comparison)
    return [
        {k: v for k, v in item.items() if k != "score_val"}
        for item in seen_paths.values()
    ]


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Render the home page with navigation to search and chat."""
    # Refresh document counts on home page load
    _update_document_counts(state.vault_path)
    return templates.TemplateResponse(
        "home.html", {
            "request": request,
            "vault_path": state.vault_path,
            "full_documents_count": state.full_documents_count,
            "shingled_chunks_count": state.shingled_chunks_count,
            "fulltext_documents_count": state.fulltext_documents_count,
        }
    )


@app.post("/refresh", response_class=HTMLResponse)
async def refresh(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Refresh pipelines and document counts, then redirect to home."""
    _refresh_pipelines(force=True)
    _update_document_counts(state.vault_path)
    # Redirect to home page
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=303)


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Render the semantic search interface page."""
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "vault_path": state.vault_path,
            "query": "",
            "results": None,
            "full_documents_count": state.full_documents_count,
            "shingled_chunks_count": state.shingled_chunks_count,
            "fulltext_documents_count": state.fulltext_documents_count,
        },
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
        {
            "request": request,
            "vault_path": state.vault_path,
            "query": query,
            "results": results,
            "error": error,
            "full_documents_count": state.full_documents_count,
            "shingled_chunks_count": state.shingled_chunks_count,
            "fulltext_documents_count": state.fulltext_documents_count,
        },
    )


@app.get("/keyword-search", response_class=HTMLResponse)
async def keyword_search_page(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Render the keyword search interface page."""
    return templates.TemplateResponse(
        "keyword_search.html",
        {
            "request": request,
            "vault_path": state.vault_path,
            "query": "",
            "results": None,
            "full_documents_count": state.full_documents_count,
            "shingled_chunks_count": state.shingled_chunks_count,
            "fulltext_documents_count": state.fulltext_documents_count,
        },
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

    if query.strip() and state.keyword_search_pipeline:
        try:
            # Refresh pipelines to ensure fresh database connections
            _refresh_pipelines()
            # Update document counts to reflect latest state
            _update_document_counts(state.vault_path)
            # Perform search with refreshed pipeline
            raw_results = list(state.keyword_search_pipeline(query))
            results = _process_keyword_results(raw_results)
        except Exception as e:
            error = str(e)

    return templates.TemplateResponse(
        "keyword_search.html",
        {
            "request": request,
            "vault_path": state.vault_path,
            "query": query,
            "results": results,
            "error": error,
            "full_documents_count": state.full_documents_count,
            "shingled_chunks_count": state.shingled_chunks_count,
            "fulltext_documents_count": state.fulltext_documents_count,
        },
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request, state: AppState = Depends(get_state)
) -> HTMLResponse:
    """Render the Ask interface page."""
    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "vault_path": state.vault_path,
            "messages": [],
            "full_documents_count": state.full_documents_count,
            "shingled_chunks_count": state.shingled_chunks_count,
            "fulltext_documents_count": state.fulltext_documents_count,
        },
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
        {
            "request": request,
            "vault_path": state.vault_path,
            "messages": messages,
            "error": error,
            "full_documents_count": state.full_documents_count,
            "shingled_chunks_count": state.shingled_chunks_count,
            "fulltext_documents_count": state.fulltext_documents_count,
        },
    )


def run_app(vault_path: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    """
    Start the web application server.

    Args:
        vault_path: Base path for vault storage. Vector DB is located at
            vault_path/vector_vault, full-text index at vault_path/fulltext_vault.
        host: Host to bind to (default: 127.0.0.1)
        port: Port to listen on (default: 8000)
    """
    init_pipelines(vault_path)
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    """CLI entry point for vault query web application."""
    parser = argparse.ArgumentParser(
        description="Web application for searching and chatting with your vault"
    )
    parser.add_argument(
        "vault_path",
        help="Path to vault storage directory",
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

    args = parser.parse_args()
    run_app(args.vault_path, args.host, args.port)


if __name__ == "__main__":
    main()
