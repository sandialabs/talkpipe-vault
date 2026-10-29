"""The vault's agent tool pack: retrieval tools for talkpipe-agents' findings loop.

Four tools let an agent search a vault for what a question should be
answered from:

- ``vector_search`` / ``keyword_search`` — the vault's semantic and Whoosh
  searches, callable any number of times with any query. They are *finding
  tools*: each returns its hits as findings, which talkpipe-agents' findings
  loop judges against the question before the agent sees them (a batched LLM
  call costing the agent no turn), keeping the relevant ones and telling the
  agent what each kept one contributes. The agent says in ``looking_for``
  what the search should find that the answer still lacks, so a partial
  answer counts;
- ``read_document`` — loads the whole document a finding came from (in
  parts, when it is long), on the hunch that more of it is useful; the part
  read is one finding, judged like a search hit;
- ``extract_keywords`` — a plain tool: asks an LLM to distill a question into
  a Whoosh query, so the agent need not write Whoosh syntax itself.

Everything else a run needs is talkpipe-agents' and is resolved by name: the
session tools ``enough_information`` (how a run ends), ``keep_finding`` and
``ignore_finding`` (deciding by hand, for a run without a judge; the old
names ``keep_chunk`` and ``ignore_chunk`` still resolve to them), and the
session, judge and hook in ``talkpipe_agents.findings`` that hold one
question's state, decide on every finding and enforce the stop.

The tools search through a :class:`SearchBackend` — plain callables, so the
pack works over any search, with no vault and no model. They hold no
per-question state, so one set serves every question; a tool that needs the
running session (``read_document``, to find the document behind an id) gets
it from Strands' tool context. ``TOOL_FACTORIES`` maps each name to a factory
taking the backend, and :func:`build_tools` resolves a list of names against
it, falling through to talkpipe-agents' own registry for anything it does
not recognize. So a pipeline picks its tools by name:

    AgentVaultSearch(vault_path=..., tools="vector_search,enough_information,think")

and a name that is neither a vault tool nor a registered generic one fails
at construction, which for a ChatterLang script means at compile time.

Nothing here knows about agents, models, or prompts — the segments in
``agent_search.py`` own that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from strands import tool as strands_tool
from strands.types.tools import ToolContext
from talkpipe.search.abstract import SearchResult
from talkpipe_agents import Finding, FindingsSession, finding_tool, resolve_agent_tools
from talkpipe_agents.findings import DEFAULT_MAX_CHARS, SESSION_KEY

from .judge_results import search_findings

logger = logging.getLogger(__name__)

# How much of a finding the agent (and the judge) is shown: the findings
# loop's default, enough for a whole chunk at the default indexing settings
# (three 300-character pieces per shingle).
CHUNK_PREVIEW_CHARS = DEFAULT_MAX_CHARS
# How much of a document one read_document call returns. Long documents are
# read in parts of this size; the part is what is kept for the answer.
DOCUMENT_PART_CHARS = 6000

SearchFn = Callable[[str], Iterable[Any]]
# A factory takes the backend the tool searches with and returns a Strands tool.
ToolFactory = Callable[["SearchBackend"], Any]

# Bound-tool factories, by the name a pipeline (or ChatterLang script) uses.
# Populated by @vault_agent_tool below.
TOOL_FACTORIES: dict[str, ToolFactory] = {}

# The pack the interfaces use. keep_finding and ignore_finding are left out:
# the findings loop judges every search's hits, and an agent given the tools
# to decide by hand spends turns on it or skips it.
DEFAULT_TOOL_NAMES = (
    "vector_search",
    "keyword_search",
    "extract_keywords",
    "read_document",
    "enough_information",
)

# Names the pack used before the findings loop, for the session tools that
# replaced them.
TOOL_ALIASES = {"keep_chunk": "keep_finding", "ignore_chunk": "ignore_finding"}


@dataclass
class SearchBackend:
    """What the vault tools search with.

    ``vector_search`` / ``keyword_search`` take a query and return search
    hits (SearchResult objects or dicts with doc_id/score/document);
    ``keyword_index_exists`` says whether keyword search can run at all
    (checked on every call, so an index built mid-run is found);
    ``extract_keywords`` turns a question into a keyword query;
    ``read_document`` takes the source a hit's document records (its
    ``source`` or ``path``) and returns that document's whole text;
    ``rename_fields`` is applied to each hit's document (``path`` → ``source``
    and ``filename`` → ``title`` make Whoosh hits citable). Any callable may
    be None, and the tool that needs it then tells the agent it is
    unavailable. They may be called from several threads at once: Strands
    runs one turn's tool calls concurrently.
    """

    vector_search: SearchFn | None = None
    keyword_search: SearchFn | None = None
    keyword_index_exists: Callable[[], bool] | None = None
    extract_keywords: Callable[[str], str] | None = None
    read_document: Callable[[str], str] | None = None
    rename_fields: dict[str, str] = field(default_factory=dict)


def vault_agent_tool(name: str) -> Callable[[ToolFactory], ToolFactory]:
    """Register a backend-bound tool factory under *name*."""

    def register(factory: ToolFactory) -> ToolFactory:
        TOOL_FACTORIES[name] = factory
        return factory

    return register


def vault_agent_tool_names() -> list[str]:
    """Every registered vault tool name, in registration order."""
    return list(TOOL_FACTORIES)


def default_tool_names() -> list[str]:
    """The default pack: the tools an agent gets when none are named."""
    return list(DEFAULT_TOOL_NAMES)


def build_tools(backend: SearchBackend, names: Iterable[str]) -> list[Any]:
    """Tool objects for *names*, searching through *backend*.

    A name in ``TOOL_FACTORIES`` becomes a vault tool; anything else is
    looked up in talkpipe-agents' global registry — the session tools
    (``enough_information``, ``keep_finding``, ``ignore_finding``) and generic
    tools (``think``, ``calculator``, …) — so a pipeline can mix them in by
    name without any code here. That fall-through is a deliberate
    developer/ChatterLang escape hatch — the interfaces only ever ask for the
    default pack, so a generic file-reading tool cannot appear behind a
    checkbox and sidestep the vault's path fences. Unknown names raise, here
    rather than mid-run.
    """
    tools: list[Any] = []
    generic: list[str] = []
    for name in names:
        name = TOOL_ALIASES.get(name, name)
        factory = TOOL_FACTORIES.get(name)
        if factory is None:
            generic.append(name)
            continue
        tools.append(factory(backend))
    if generic:
        # resolve_agent_tools raises on an unknown name, listing what exists.
        try:
            tools.extend(resolve_agent_tools(generic))
        except ValueError as exc:
            raise ValueError(
                f"{exc}; vault retrieval tools: {', '.join(vault_agent_tool_names())}"
            ) from exc
    return tools


def parse_tool_names(spec: str | Iterable[str] | None) -> list[str]:
    """Normalize a tool list given as a comma-separated string or a sequence.

    None (or an empty spec) means the default pack, so a caller that does
    not care about tool selection gets the working default.
    """
    if spec is None:
        return default_tool_names()
    names = spec.split(",") if isinstance(spec, str) else list(spec)
    cleaned = [str(name).strip() for name in names if str(name).strip()]
    return cleaned or default_tool_names()


def session_of(tool_context: Any) -> FindingsSession | None:
    """The findings session running the tool call, if any: the loop's hook
    hands it to the tools through Strands' invocation state."""
    state = getattr(tool_context, "invocation_state", None)
    session = state.get(SESSION_KEY) if isinstance(state, dict) else None
    return session if isinstance(session, FindingsSession) else None


def _tell(tool_context: Any, message: str) -> None:
    session = session_of(tool_context)
    if session is not None:
        session.tell(message)
    else:
        logger.info(message)


def _finding_tool(func: Callable[..., Any]) -> Any:
    """``finding_tool``, unregistered: each vault tool is bound to one backend,
    so it is resolved through ``TOOL_FACTORIES`` rather than the global
    registry."""
    return finding_tool(func, register=False)


def _search(
    kind: str, search: SearchFn | None, query: str, backend: SearchBackend
) -> list[Finding]:
    # A refusal is raised rather than returned: a finding tool's return value
    # is findings, and the agent reads an error result as the tool's reply.
    query = query.strip()
    if not query:
        raise ValueError("The query is empty.")
    if search is None:
        raise ValueError(f"The {kind} search is unavailable here.")
    try:
        hits = list(search(query) or [])
    except Exception as exc:
        logger.warning("Agent %s search failed: %s", kind, exc, exc_info=exc)
        raise RuntimeError(f"The {kind} search failed: {exc}") from exc
    return search_findings(hits, backend.rename_fields)


@vault_agent_tool("vector_search")
def make_vector_search(backend: SearchBackend) -> Any:
    @_finding_tool
    def vector_search(query: str, looking_for: str = "") -> list[Finding]:
        """Semantic search over the vault: finds chunks whose meaning is
        close to the query, even when the words differ. Best with a
        full question or descriptive phrase. The results come back
        already judged against the question: the relevant ones are kept
        for the answer, the rest ignored, and you are told what each kept
        finding contributes and what is already known.

        Args:
            query: A natural-language question or descriptive phrase.
            looking_for: What this search should find that the answer
                still lacks, in one phrase, so a result supplying just
                that is kept. Leave it empty for the question itself.
        """
        return _search("vector", backend.vector_search, query, backend)

    return vector_search


@vault_agent_tool("keyword_search")
def make_keyword_search(backend: SearchBackend) -> Any:
    @_finding_tool
    def keyword_search(query: str, looking_for: str = "") -> list[Finding]:
        """Exact-term search over the vault's full-text index: finds
        chunks containing the given words. Best for names, identifiers,
        and specific terms. Whoosh syntax: terms are ANDed, use OR for
        alternatives and double quotes for phrases. Reports when no
        keyword index exists yet. The results come back already judged
        against the question, like vector_search's.

        Args:
            query: The keyword query, in Whoosh syntax.
            looking_for: What this search should find that the answer
                still lacks, in one phrase, so a result supplying just
                that is kept. Leave it empty for the question itself.
        """
        exists = backend.keyword_index_exists
        if backend.keyword_search is not None and exists is not None and not exists():
            raise ValueError(
                "This vault has no full-text index, so keyword search is "
                "unavailable; use vector_search instead."
            )
        return _search("keyword", backend.keyword_search, query, backend)

    return keyword_search


@vault_agent_tool("extract_keywords")
def make_extract_keywords(backend: SearchBackend) -> Any:
    @strands_tool(context=True)
    def extract_keywords(question: str, tool_context: ToolContext | None = None) -> str:
        """Ask a language model to turn a question or topic into a
        keyword query for keyword_search. Use it when you want good
        search-engine terms for a plain-language question instead of
        writing the query yourself. Returns the query; pass it to
        keyword_search (edit it first if you want).

        Args:
            question: The question or topic, in plain language.
        """
        # Not a finding tool (it finds nothing), so it checks the stop itself.
        session = session_of(tool_context)
        if session is not None and (refusal := session.guard("extract_keywords")):
            return str(refusal)
        question = question.strip()
        if not question:
            return "The question is empty."
        if backend.extract_keywords is None:
            return (
                "Keyword extraction is unavailable here; write the keyword "
                "query yourself."
            )
        try:
            query = backend.extract_keywords(question)
        except Exception as exc:
            logger.warning("Agent keyword extraction failed", exc_info=True)
            _tell(tool_context, f"agent ← extract_keywords failed: {exc}")
            return (
                f"Keyword extraction failed: {exc}. Write the keyword query yourself."
            )
        query = str(query).strip()
        if not query:
            _tell(tool_context, "agent ← extract_keywords: no keywords found")
            return "No keywords could be extracted; write the keyword query yourself."
        _tell(tool_context, f"agent ← extract_keywords: {query}")
        return (
            f"Keyword query: {query}\nPass it to keyword_search, editing it if "
            "you want different terms."
        )

    return extract_keywords


def read_part(
    backend: SearchBackend, finding: Finding, finding_id: str, start: int | None
) -> Finding:
    """One part of the document *finding* came from, as a finding of its own.

    The part is keyed by document and offset, so reading it again is the
    same finding, and its hit carries the source as ``doc_id`` so the
    interfaces can open the cited document. The label says which characters
    it covers, which is how the agent knows where to read on.
    """
    result = finding.to_search_result()
    doc = result.document or {}
    source = finding.source.strip()
    if not source:
        raise ValueError(f"{finding_id} records no source document to read.")
    if backend.read_document is None:
        raise ValueError("Reading whole documents is unavailable here.")
    try:
        text = backend.read_document(source).strip()
    except Exception as exc:
        logger.warning("Agent document read failed", exc_info=True)
        raise RuntimeError(f"The document could not be read: {exc}") from exc
    start = max(0, start or 0)
    if start >= len(text):
        raise ValueError(
            f"The document has {len(text)} characters; there is nothing from "
            f"character {start} on."
        )
    part = text[start : start + DOCUMENT_PART_CHARS]
    end = start + len(part)
    fields = {
        k: v
        for k, v in doc.items()
        if k not in {"content", "shingle", "shingle_text", "_doc_id", "doc_id"}
    }
    title = finding.label or source
    return Finding(
        part,
        label=f"{title}, characters {start}-{end} of {len(text)}",
        source=source,
        key=f"document:{source}:{start}",
        meta={
            "result": SearchResult(
                score=result.score, doc_id=source, document={**fields, "content": part}
            )
        },
    )


@vault_agent_tool("read_document")
def make_read_document(backend: SearchBackend) -> Any:
    @_finding_tool
    def read_document(
        finding_id: str,
        start: int | None = None,
        looking_for: str = "",
        tool_context: ToolContext | None = None,
    ) -> Finding:
        """Read the whole document a finding came from, when you suspect
        more of it would help: the finding is cut off, it is part of a list
        or a procedure, or its document looks like the main place the
        question is covered. The part read becomes a new finding, judged
        against the question like a search result. Long documents come in
        parts, each labelled with the characters it covers.

        Args:
            finding_id: The id of a finding from that document, e.g. f3.
            start: Character offset to read from; leave it out to read from
                the beginning, or pass where a part you read ended to read
                on.
            looking_for: What you hope this part of the document holds, in
                one phrase, so it is kept when it supplies just that.
        """
        # The agent names a finding, never a path, so only documents the
        # vault itself returned can be read.
        finding_id = finding_id.strip()
        session = session_of(tool_context)
        finding = session.finding(finding_id) if session is not None else None
        if finding is None:
            raise ValueError(
                f"Unknown finding id {finding_id!r}; use an id from a tool reply "
                "you have already received (a call made in the same step has "
                "not returned its ids yet)."
            )
        return read_part(backend, finding, finding_id, start)

    return read_document
