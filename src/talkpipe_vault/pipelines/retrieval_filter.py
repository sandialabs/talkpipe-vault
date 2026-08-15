"""Per-vault retrieval-filter script storage and validation (issue #22).

A vault may carry a user-supplied ChatterLang script that filters and/or
transforms retrieved search results before they reach the RAG prompt. The
script text lives inside the vault directory (``retrieval_filter.tps``) so it
travels when the vault is copied or moved — like ``vault_metadata.json``, it
describes something about this vault's data ("these results need pruning"),
not about the machine.

Whether the script actually *runs* is deliberately not stored here: activation
is a per-machine flag, kept per vault, in the app's user settings
(``user_settings.get_retrieval_filter_flags``, keyed by vault path). Enabling
one vault's filter therefore never enables another's. A vault received from
someone else never executes its bundled script until the user reviews and
enables it — ChatterLang segments can call LLMs and evaluate expressions, so
running one on open would let a copied vault execute someone else's code.

Scripts see each result as a plain dict — ``{"doc_id", "score", "document"}``
— so a ``lambda``/``lambdaFilter`` expression can address it as ``item``
(``item['document']``), the usual TalkPipe name, or through the bare top-level
keys TalkPipe exposes for dict items (``document``, ``score``, ``doc_id``).
Containment tests need no expression at all: ``isIn``/``isNotIn`` take the
field as a dotted path (``field="document.content"``) and the text to look
for. They match case-sensitively and raise when a result lacks the named
field, so a filter over a field that may be absent, or one that should ignore
case, still wants ``lambdaFilter``.

Scripts must be a single segment-only pipeline: no sources (the retrieval
stream is the input), no loops, no forks. ``validate_script`` enforces this
and surfaces ChatterLang compile errors as user-readable messages.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from parsy import ParseError
from talkpipe.chatterlang import compiler
from talkpipe.chatterlang.parsers import ParsedPipeline, script_parser

logger = logging.getLogger(__name__)

FILTER_FILENAME = "retrieval_filter.tps"

# When a filter is active, retrieval fetches this many times the wanted result
# count so the page/prompt can still fill up after the filter drops results;
# each filtered stream is truncated back to its limit afterwards.
FILTER_OVERFETCH_FACTOR = 3

_STRUCTURE_HINT = (
    "The retrieval filter must be a single pipeline of segments that reads "
    'the result stream, e.g. starting with "| lambdaFilter[...]" — without '
    "an INPUT FROM source, loops, or forks."
)


def script_path(vault_path: str) -> Path:
    """Return the path of the vault's retrieval-filter script file."""
    return Path(vault_path) / FILTER_FILENAME


def load_script(vault_path: str) -> str | None:
    """Return the vault's filter script text, or None when absent or blank.

    Best effort: an unreadable file is logged and treated as absent, so a
    broken sidecar can never keep a vault from opening.
    """
    path = script_path(vault_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not read retrieval filter at %s: %s", path, exc)
        return None
    return text if text.strip() else None


def save_script(vault_path: str, text: str) -> None:
    """Write the vault's filter script (atomically, like the metadata sidecar)."""
    path = script_path(vault_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tps.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def remove_script(vault_path: str) -> bool:
    """Delete the vault's filter script file. Returns True when one existed."""
    try:
        script_path(vault_path).unlink()
    except FileNotFoundError:
        return False
    return True


def validate_script(text: str) -> str | None:
    """Return a user-readable error for an unusable script, or None when valid.

    Checks, in order: the script compiles (ChatterLang syntax, known segment
    names, valid parameters), and it is structurally a single segment-only
    pipeline — no source, loops, or forks — so it can be spliced into the
    retrieval stream.
    """
    if not text or not text.strip():
        return "The script is empty."

    stripped = compiler.remove_comments(text)
    try:
        compiler.compile(text)
        parsed = script_parser.parse(stripped)
    except compiler.CompileError as exc:
        return str(exc)
    except ParseError as exc:  # compile succeeded but reparse failed: unexpected
        return f"Could not parse the script: {exc}"
    except Exception as exc:  # segment __init__ can raise anything
        return f"The script failed to compile: {exc}"

    pipelines = parsed.pipelines
    if len(pipelines) != 1 or not isinstance(pipelines[0], ParsedPipeline):
        return _STRUCTURE_HINT
    pipeline = pipelines[0]
    if pipeline.input_node is not None:
        return (
            "The script must not include its own input source — the retrieved "
            "results are the input. Remove the INPUT FROM clause and start "
            'the script with "|". ' + _STRUCTURE_HINT
        )
    if pipeline.fork_source or pipeline.fork_target:
        return _STRUCTURE_HINT
    return None


def compile_script(text: str) -> Callable[[Any], Any]:
    """Validate and compile a filter script into a stream-transforming callable.

    Raises ValueError with the ``validate_script`` message when the script is
    unusable, so callers get one failure mode to handle.
    """
    error = validate_script(text)
    if error:
        raise ValueError(error)
    compiled: Callable[[Any], Any] = compiler.compile(text)
    return compiled
