"""Search results and answers as both interfaces present them.

Raw hits from the semantic, keyword and agent pipelines become display rows
(path, lookup key, title, snippet, score); the agent branch the Search and
Ask pages share lives here so the two interfaces cannot drift apart; and a
source document is served inline only when a browser can render it safely.
"""

import mimetypes
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse

from talkpipe_vault.apps.pipelines import agent_progress
from talkpipe_vault.apps.state import (
    DEFAULT_RAG_RESULT_LIMIT,
    AppState,
    _effective_models,
    _state,
)
from talkpipe_vault.pipelines import agent_search

SNIPPET_MAX_LENGTH = 300


# Wording shared by the web Ask page and the terminal interface for the two
# things an agent run cannot do: run when its model could not be built, and
# apply the vault's retrieval filter (the agent searches through its own
# tools, which the filter never sees). Kept here so both interfaces say the
# same thing and neither silently ignores the request.
AGENT_UNAVAILABLE_NOTE = "the agent is unavailable right now — answered without it"
AGENT_FILTER_NOTE = (
    "the vault's retrieval filter does not apply to the agent's selection"
)
EXTRA_EFFORT_NOTE = "Extra Effort"
AGENT_SEARCH_FILTER_NOTE = (
    "The custom transform does not apply to the agent's selection and was not run."
)


def _answer_with_agent(
    state: AppState,
    message: str,
    extra_effort: bool = False,
    progress: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any]], list[str], str]:
    """Answer *message* through the agent pipeline.

    Returns (response, citations, trace, note): the answer, the chunks the
    agent kept — exactly the RAG context, so they are the citations — the
    tool-call trace, and the note on how the run ended. Both interfaces call
    this so their agent branches cannot drift apart. *extra_effort* runs the
    agent with the raised iteration cap. *progress*, when given, is also
    called with each trace line as the run works.
    """
    pipeline = state.agent_chat_pipeline
    if extra_effort and state.agent_chat_extra_pipeline:
        pipeline = state.agent_chat_extra_pipeline
    if pipeline is None:
        # Callers check availability first; this is the type-narrowing guard,
        # written as a real check rather than an assert so it survives -O and
        # satisfies bandit (B101).
        raise RuntimeError("the agent pipeline is not available")
    token = agent_progress.set(progress)
    try:
        outcome = pipeline(message)
    finally:
        agent_progress.reset(token)
    note = outcome.note
    if pipeline is state.agent_chat_extra_pipeline:
        note = _prefix_note(note, EXTRA_EFFORT_NOTE)
    return (
        outcome.response,
        _process_semantic_results(outcome.results),
        outcome.trace,
        note,
    )


def _select_with_agent(
    state: AppState, text: str, extra_effort: bool = False
) -> agent_search.AgentSearchOutcome:
    """Run the Search page's agent selection for *text*.

    The Search route and ``VaultService.search`` share it, as they share
    ``_answer_with_agent``. *extra_effort* runs the agent with the raised
    iteration cap, and the outcome's note then says so.
    """
    pipeline = state.agent_search_pipeline
    if extra_effort and state.agent_search_extra_pipeline:
        pipeline = state.agent_search_extra_pipeline
    if pipeline is None:
        raise RuntimeError("the agent pipeline is not available")
    outcome = pipeline(text)
    if pipeline is state.agent_search_extra_pipeline:
        outcome.note = f"{EXTRA_EFFORT_NOTE}: {outcome.note}"
    return outcome


def _prefix_note(note: str, prefix: str) -> str:
    """Put *prefix* in front of a possibly empty "Answered by" note."""
    return f"{prefix} · {note}" if note else prefix


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


def _with_folder(title: str, document: Any) -> str:
    """A result's title, followed by its folder's name when several folders
    are open, so same-named files from different folders can be told apart
    without showing paths."""
    folder = _get_field(document, "folder", "")
    if folder and len(_state.folders) > 1:
        return f"{title} — {folder}"
    return title


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
                "filename": _with_folder(_resolve_title(title, filename, path), doc),
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
    limit = state.rag_result_limit or DEFAULT_RAG_RESULT_LIMIT
    return _process_semantic_results(raw_results)[:limit]


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
                "filename": _with_folder(
                    _resolve_title(title, filename, path), document
                ),
                "snippet": _create_snippet(content),
                "score": f"{score:.4f}",
            }
        )

    return results


# Media types the browser may render directly. Everything else — notably
# text/html and image/svg+xml, which could run scripts with the app's origin —
# is served as a download instead.
_INLINE_MEDIA_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}

# Text-like documents shown in-browser as plain text, whatever their real
# media type (browsers download text/markdown and friends otherwise).
_INLINE_TEXT_EXTENSIONS = {".txt", ".text", ".md", ".markdown", ".rst", ".log"}


def _document_file_response(source_path: Path) -> FileResponse:
    """Serve a source document, inline when the browser can safely render it."""
    if source_path.suffix.lower() in _INLINE_TEXT_EXTENSIONS:
        return FileResponse(
            source_path,
            media_type="text/plain; charset=utf-8",
            filename=source_path.name,
            content_disposition_type="inline",
        )
    media_type, _ = mimetypes.guess_type(source_path.name)
    if media_type in _INLINE_MEDIA_TYPES:
        return FileResponse(
            source_path,
            media_type=media_type,
            filename=source_path.name,
            content_disposition_type="inline",
        )
    return FileResponse(source_path, filename=source_path.name)


def _filter_outcome_note(count: int, filter_error: str | None) -> str:
    """Describe the outcome of an opt-in filtered search for the results page."""
    if filter_error:
        return (
            "The custom transform failed and was skipped — showing unfiltered "
            f"results ({filter_error})."
        )
    return (
        f"Custom transform applied — {count} result{'s' if count != 1 else ''} "
        "after filtering."
    )
