"""
Web application for querying and chatting with vault contents.

Provides two interaction modes via web interface:
- Search: Search engine-like interface returning ranked results
- Chat: Conversational RAG-based interface for Q&A
"""
import argparse
import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from talkpipe.util.config import configure_logger

from talkpipe_vault.pipelines.searching_and_prompting import VaultSearch, VaultChat

configure_logger("root:ERROR")

# Get the templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Vault Query", description="Search and chat with your vault")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Global state for vault path and pipelines
_vault_path: str = ""
_search_pipeline = None
_chat_pipeline = None


def init_pipelines(vault_path: str) -> None:
    """
    Initialize the search and chat pipelines.

    Expects vault_path as a string pointing to a LanceDB database directory
    containing 'shingled_chunks' table with embedded document chunks.
    """
    global _vault_path, _search_pipeline, _chat_pipeline
    _vault_path = vault_path
    _search_pipeline = VaultSearch(path=vault_path).as_function(
        single_in=True, single_out=True
    )
    _chat_pipeline = VaultChat(path=vault_path).as_function(
        single_in=True, single_out=True
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """
    Render the home page with navigation to search and chat.

    Returns HTML page with links to both interfaces.
    """
    return templates.TemplateResponse(
        "home.html", {"request": request, "vault_path": _vault_path}
    )


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request) -> HTMLResponse:
    """
    Render the search interface page.

    Returns HTML page with search input form and results area.
    """
    return templates.TemplateResponse(
        "search.html",
        {"request": request, "vault_path": _vault_path, "query": "", "results": None},
    )


@app.post("/search", response_class=HTMLResponse)
async def search_results(
    request: Request, query: Annotated[str, Form()]
) -> HTMLResponse:
    """
    Process a search query and return results.

    Expects form data with:
        - query: str - The search query string

    Returns HTML page with search results containing:
        - path: Source file path
        - shingle: Matched text content
        - score: Similarity score (1 - distance)
    """
    results = []
    error = None

    if query.strip():
        try:
            raw_results = _search_pipeline(query)

            if raw_results:
                if isinstance(raw_results, dict):
                    raw_results = [raw_results]

                for result in raw_results:
                    # SearchResult objects have document dict and score attributes
                    doc = result.document if hasattr(result, "document") else result
                    path = doc.get("path", "Unknown") if isinstance(doc, dict) else getattr(doc, "path", "Unknown")
                    shingle = doc.get("shingle", "") if isinstance(doc, dict) else getattr(doc, "shingle", "")
                    # Use score (1 - distance) or _distance depending on result type
                    if hasattr(result, "score"):
                        distance = 1 - result.score  # Convert score back to distance
                    elif hasattr(result, "_distance"):
                        distance = result._distance
                    elif isinstance(result, dict):
                        distance = result.get("_distance", 0)
                    else:
                        distance = 0

                    # Create snippet
                    snippet = shingle[:300].replace("\n", " ").strip()
                    if len(shingle) > 300:
                        snippet += "..."

                    results.append(
                        {
                            "path": path,
                            "filename": Path(path).name if path else "Unknown",
                            "snippet": snippet,
                            "score": f"{(1 - distance):.4f}",
                        }
                    )
        except Exception as e:
            error = str(e)

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "vault_path": _vault_path,
            "query": query,
            "results": results,
            "error": error,
        },
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request) -> HTMLResponse:
    """
    Render the chat interface page.

    Returns HTML page with chat input form and conversation area.
    """
    return templates.TemplateResponse(
        "chat.html",
        {"request": request, "vault_path": _vault_path, "messages": []},
    )


@app.post("/chat", response_class=HTMLResponse)
async def chat_response(
    request: Request, message: Annotated[str, Form()]
) -> HTMLResponse:
    """
    Process a chat message and return AI response.

    Expects form data with:
        - message: str - The user's question

    Returns HTML page with conversation containing user message and AI response.
    """
    messages = []
    error = None

    if message.strip():
        messages.append({"role": "user", "content": message})

        try:
            response = _chat_pipeline(message)
            messages.append({"role": "assistant", "content": response})
        except Exception as e:
            error = str(e)

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "vault_path": _vault_path,
            "messages": messages,
            "error": error,
        },
    )


def run_app(vault_path: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    """
    Start the web application server.

    Expects:
        - vault_path: str - Path to LanceDB vault database
        - host: str - Host to bind to (default: 127.0.0.1)
        - port: int - Port to listen on (default: 8000)
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
        help="Path to LanceDB vault database",
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
