"""The vault's agent tool pack: retrieval tools an ``llmAgent`` can call.

Six tools let an agent search a vault and decide, chunk by chunk, what a
question should be answered from:

- ``vector_search`` / ``keyword_search`` — the vault's semantic and Whoosh
  searches, callable any number of times with any query;
- ``extract_keywords`` — asks an LLM to distill a question into a Whoosh
  query, so the agent need not write Whoosh syntax itself;
- ``keep_chunk`` / ``ignore_chunk`` — the decisions;
- ``enough_information`` — how a run ends: the agent's own judgement that
  what it kept would answer the question well.

All six need per-query state (which chunks have been seen, what was kept),
so they are **factories** returning tools bound to one
:class:`AgentSearchSession` — the pattern talkpipe-agents documents for tool
packs that need runtime state. ``TOOL_FACTORIES`` maps each name to its
factory the way talkpipe-agents' registry maps names to tool objects, and
:func:`build_tools` resolves a list of names against it, falling through to
talkpipe-agents' own registry for anything it does not recognize. So a
pipeline picks its tools by name:

    AgentVaultSearch(vault_path=..., tools="vector_search,keep_chunk,think")

and a name that is neither a vault tool nor a registered generic one fails
at construction, which for a ChatterLang script means at compile time.

Nothing here knows about agents, models, or prompts — the segments in
``agent_search.py`` own that. This module owns only what the tools do and
the state they share, which is what would move to a generic package if
these tools were ever pulled out of the vault.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from strands import tool as strands_tool
from talkpipe.search.abstract import SearchResult
from talkpipe_agents import resolve_agent_tools

from .searching_and_prompting import normalize_search_result

logger = logging.getLogger(__name__)

# How much of a chunk the agent is shown when a search returns it.
CHUNK_PREVIEW_CHARS = 500

SearchFn = Callable[[str], Iterable[Any]]
# A factory takes the session the tool will share and returns a Strands tool.
ToolFactory = Callable[["AgentSearchSession"], Any]

# Bound-tool factories, by the name a pipeline (or ChatterLang script) uses.
# Populated by @vault_agent_tool below.
TOOL_FACTORIES: dict[str, ToolFactory] = {}


def vault_agent_tool(name: str) -> Callable[[ToolFactory], ToolFactory]:
    """Register a session-bound tool factory under *name*."""

    def register(factory: ToolFactory) -> ToolFactory:
        TOOL_FACTORIES[name] = factory
        return factory

    return register


def vault_agent_tool_names() -> list[str]:
    """The vault tool names, in the order the default pack lists them."""
    return list(TOOL_FACTORIES)


def build_tools(session: AgentSearchSession, names: Iterable[str]) -> list[Any]:
    """Tool objects for *names*, bound to *session*.

    A name in ``TOOL_FACTORIES`` becomes a session-bound vault tool; anything
    else is looked up in talkpipe-agents' global registry, so a pipeline can
    mix in generic tools (``think``, ``calculator``, …) by name without any
    code here. That fall-through is a deliberate developer/ChatterLang escape
    hatch — the interfaces only ever ask for the default pack, so a generic
    file-reading tool cannot appear behind a checkbox and sidestep the vault's
    path fences. Unknown names raise, here rather than mid-run.
    """
    tools: list[Any] = []
    generic: list[str] = []
    for name in names:
        factory = TOOL_FACTORIES.get(name)
        if factory is None:
            generic.append(name)
            continue
        tools.append(factory(session))
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

    None (or an empty spec) means the full vault pack, so a caller that does
    not care about tool selection gets the working default.
    """
    if spec is None:
        return vault_agent_tool_names()
    names = spec.split(",") if isinstance(spec, str) else list(spec)
    cleaned = [str(name).strip() for name in names if str(name).strip()]
    return cleaned or vault_agent_tool_names()


@dataclass
class _Chunk:
    id: str
    result: SearchResult
    key: str


class AgentSearchSession:
    """One query's worth of shared tool state: the chunks discovered so far,
    the keep/ignore decisions, and whether the agent has declared it has
    enough to answer.

    The search callables are injected so the session (and every tool bound to
    it) can be exercised without a vault or an LLM:
    ``vector_search``/``keyword_search`` take a query and return search hits
    (SearchResult objects or dicts with doc_id/score/document);
    ``keyword_index_exists`` says whether keyword search can run at all;
    ``extract_keywords`` asks an LLM to turn a question into a keyword query
    (None when no model is available).
    """

    def __init__(
        self,
        *,
        vector_search: SearchFn,
        keyword_search: SearchFn,
        keyword_index_exists: Callable[[], bool],
        extract_keywords: Callable[[str], str] | None,
        max_chunks: int,
        tell: Callable[[str], None],
        rename_fields: dict[str, str] | None = None,
    ) -> None:
        self._vector_search = vector_search
        self._keyword_search = keyword_search
        self._keyword_index_exists = keyword_index_exists
        self._extract_keywords = extract_keywords
        self.max_chunks = max(1, max_chunks)
        self._tell = tell
        self._rename_fields = rename_fields or {}
        self._chunks: dict[str, _Chunk] = {}
        self._by_key: dict[str, str] = {}
        self.kept: list[str] = []
        self.ignored: set[str] = set()
        self.trace: list[str] = []
        self.queries_run: list[tuple[str, str]] = []
        # Set by the enough_information tool: the agent's own judgement that
        # what it has kept would answer the question well. This, not the
        # number of chunks, is the run's stopping criterion.
        self.enough = False
        self.enough_reason = ""

    # -- bookkeeping ---------------------------------------------------------

    def tell(self, message: str) -> None:
        self.trace.append(message)
        self._tell(message)

    @property
    def at_capacity(self) -> bool:
        """True once the run has collected as much context as it may.

        A backstop, not a goal: it bounds a run whose agent never decides it
        has enough (or keeps collecting past the point of usefulness).
        """
        return len(self.kept) >= self.max_chunks

    @property
    def stop_note(self) -> str:
        """Why the run is over, phrased for the model. Empty while it runs."""
        if self.enough:
            return "you already said you had enough"
        if self.at_capacity:
            return f"the {self.max_chunks}-chunk maximum is reached"
        return ""

    @property
    def should_stop(self) -> bool:
        return bool(self.stop_note)

    @property
    def results(self) -> list[SearchResult]:
        return [self._chunks[cid].result for cid in self.kept]

    def _register(self, entry: Any) -> _Chunk | None:
        result = normalize_search_result(entry, self._rename_fields)
        if result is None:
            return None
        key = result.doc_id or json.dumps(result.document, sort_keys=True, default=str)
        existing = self._by_key.get(key)
        if existing is not None:
            return self._chunks[existing]
        chunk = _Chunk(id=f"c{len(self._chunks) + 1}", result=result, key=key)
        self._chunks[chunk.id] = chunk
        self._by_key[key] = chunk.id
        return chunk

    def _describe(self, chunk: _Chunk) -> str:
        doc = chunk.result.document or {}
        title = str(
            doc.get("title") or doc.get("filename") or doc.get("source") or ""
        ).strip()
        text = str(doc.get("content") or doc.get("shingle") or "").strip()
        text = " ".join(text.split())
        if len(text) > CHUNK_PREVIEW_CHARS:
            text = text[:CHUNK_PREVIEW_CHARS] + "…"
        status = ""
        if chunk.id in self.kept:
            status = " (already kept)"
        elif chunk.id in self.ignored:
            status = " (already ignored)"
        head = f"[{chunk.id}]{status}"
        if title:
            head += f" {title}"
        return f"{head}\n{text}"

    def _status_line(self) -> str:
        return f"Kept {len(self.kept)} chunk(s), at most {self.max_chunks}."

    # -- tool bodies ---------------------------------------------------------

    def run_search(self, kind: str, query: str, search: SearchFn) -> str:
        query = query.strip()
        if not query:
            return "The query is empty."
        if (kind, query) in self.queries_run:
            return f"You already ran this {kind} query; try a different one."
        self.queries_run.append((kind, query))
        try:
            hits = list(search(query) or [])
        except Exception as exc:
            logger.warning("Agent %s search failed", kind, exc_info=True)
            self.tell(f"agent ← {kind}_search failed: {exc}")
            return f"The {kind} search failed: {exc}"
        chunks: list[_Chunk] = []
        seen: set[str] = set()
        new = 0
        for hit in hits:
            chunk = self._register(hit)
            if chunk is None or chunk.id in seen:
                continue
            seen.add(chunk.id)
            chunks.append(chunk)
            if chunk.id not in self.kept and chunk.id not in self.ignored:
                new += 1
        self.tell(
            f"agent ← {kind}_search: {len(chunks)} hit(s), {new} undecided. "
            + self._status_line()
        )
        if not chunks:
            return f"No {kind} results for {query!r}. Try different terms."
        body = "\n\n".join(self._describe(c) for c in chunks)
        return (
            f"{len(chunks)} result(s) for {query!r}. Decide on each undecided "
            f"chunk with keep_chunk or ignore_chunk, then judge what the "
            f"question still needs. {self._status_line()}\n\n{body}"
        )

    def vector_search(self, query: str) -> str:
        return self.run_search("vector", query, self._vector_search)

    def keyword_search(self, query: str) -> str:
        if not self._keyword_index_exists():
            self.tell("agent ← keyword_search: no keyword index exists")
            return (
                "This vault has no full-text index, so keyword search is "
                "unavailable; use vector_search instead."
            )
        return self.run_search("keyword", query, self._keyword_search)

    def extract_keywords(self, question: str) -> str:
        """Ask the LLM keyword extractor for a keyword query for *question*.

        The extractor itself already falls back to the raw question when the
        model fails or returns nothing usable, so the agent always gets
        something it can search with.
        """
        question = question.strip()
        if not question:
            return "The question is empty."
        if self._extract_keywords is None:
            return (
                "Keyword extraction is unavailable here; write the keyword "
                "query yourself."
            )
        try:
            query = self._extract_keywords(question)
        except Exception as exc:
            logger.warning("Agent keyword extraction failed", exc_info=True)
            self.tell(f"agent ← extract_keywords failed: {exc}")
            return (
                f"Keyword extraction failed: {exc}. Write the keyword query yourself."
            )
        query = str(query).strip()
        if not query:
            self.tell("agent ← extract_keywords: no keywords found")
            return "No keywords could be extracted; write the keyword query yourself."
        self.tell(f"agent ← extract_keywords: {query}")
        return (
            f"Keyword query: {query}\nPass it to keyword_search, editing it if "
            "you want different terms."
        )

    def decide(self, chunk_id: str, reason: str, *, keep: bool) -> str:
        chunk_id = chunk_id.strip()
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            return f"Unknown chunk id {chunk_id!r}; use an id from a search result."
        verb = "keep" if keep else "ignore"
        if chunk_id in self.kept:
            return f"{chunk_id} is already kept."
        if chunk_id in self.ignored:
            if not keep:
                return f"{chunk_id} is already ignored."
            self.ignored.discard(chunk_id)
        if keep:
            self.kept.append(chunk_id)
        else:
            self.ignored.add(chunk_id)
        reason = reason.strip().rstrip(".")
        why = f" — {reason}" if reason else ""
        self.tell(f"agent · {verb} {chunk_id}{why}. {self._status_line()}")
        if self.at_capacity:
            return (
                f"{verb.capitalize()} {chunk_id}. You have kept the maximum of "
                f"{self.max_chunks} chunks for this run. Stop calling tools and "
                "reply: done"
            )
        return (
            f"{verb.capitalize()} {chunk_id}. {self._status_line()} Call "
            "enough_information once what you have kept would answer the "
            "question well."
        )

    def declare_enough(self, reason: str) -> str:
        """Record the agent's judgement that it can answer the question.

        Refused while nothing has been kept: an answer needs context, and a
        run that stops with none produces no answer at all, so the agent is
        told to keep searching instead.
        """
        reason = reason.strip().rstrip(".")
        if not self.kept:
            self.tell("agent ✗ enough_information refused: nothing kept yet")
            return (
                "You have not kept any chunk yet, so there is nothing to answer "
                "from. Keep searching, and keep the chunks the answer needs."
            )
        self.enough = True
        self.enough_reason = reason
        why = f" — {reason}" if reason else ""
        self.tell(f"agent · enough information{why}. {self._status_line()}")
        return (
            f"Noted: {len(self.kept)} chunk(s) kept. Stop calling tools and reply: done"
        )


def guard(session: AgentSearchSession, name: str) -> str:
    """The refusal a tool returns once the run is over, or "" while it runs.

    The stopping rule lives with the session that owns it rather than in an
    agent hook, so the tools stay usable behind any agent — and a model that
    keeps calling tools after being told to stop gets the same answer every
    time instead of quietly collecting more.
    """
    reason = session.stop_note
    if not reason:
        return ""
    session.tell(f"agent ✗ {name} refused: {reason}")
    return f"The search is over — {reason}. Stop calling tools and reply: done"


@vault_agent_tool("vector_search")
def make_vector_search(session: AgentSearchSession) -> Any:
    @strands_tool
    def vector_search(query: str) -> str:
        """Semantic search over the vault: finds chunks whose meaning is
        close to the query, even when the words differ. Best with a
        full question or descriptive phrase. Each result carries a chunk
        id to pass to keep_chunk or ignore_chunk.

        Args:
            query: A natural-language question or descriptive phrase.
        """
        return guard(session, "vector_search") or session.vector_search(query)

    return vector_search


@vault_agent_tool("keyword_search")
def make_keyword_search(session: AgentSearchSession) -> Any:
    @strands_tool
    def keyword_search(query: str) -> str:
        """Exact-term search over the vault's full-text index: finds
        chunks containing the given words. Best for names, identifiers,
        and specific terms. Whoosh syntax: terms are ANDed, use OR for
        alternatives and double quotes for phrases. Reports when no
        keyword index exists yet.

        Args:
            query: The keyword query, in Whoosh syntax.
        """
        return guard(session, "keyword_search") or session.keyword_search(query)

    return keyword_search


@vault_agent_tool("extract_keywords")
def make_extract_keywords(session: AgentSearchSession) -> Any:
    @strands_tool
    def extract_keywords(question: str) -> str:
        """Ask a language model to turn a question or topic into a
        keyword query for keyword_search. Use it when you want good
        search-engine terms for a plain-language question instead of
        writing the query yourself. Returns the query; pass it to
        keyword_search (edit it first if you want).

        Args:
            question: The question or topic, in plain language.
        """
        return guard(session, "extract_keywords") or session.extract_keywords(question)

    return extract_keywords


@vault_agent_tool("keep_chunk")
def make_keep_chunk(session: AgentSearchSession) -> Any:
    @strands_tool
    def keep_chunk(chunk_id: str, reason: str = "") -> str:
        """Keep a chunk: it contributes information the answer needs.
        Kept chunks are what the question will be answered from.

        Args:
            chunk_id: The id shown with the chunk, e.g. c3.
            reason: One short phrase saying what it contributes.
        """
        return guard(session, "keep_chunk") or session.decide(
            chunk_id, reason, keep=True
        )

    return keep_chunk


@vault_agent_tool("ignore_chunk")
def make_ignore_chunk(session: AgentSearchSession) -> Any:
    @strands_tool
    def ignore_chunk(chunk_id: str, reason: str = "") -> str:
        """Ignore a chunk: it is not relevant to the question and must
        not be shown to the user.

        Args:
            chunk_id: The id shown with the chunk, e.g. c3.
            reason: One short phrase saying why it is irrelevant.
        """
        return guard(session, "ignore_chunk") or session.decide(
            chunk_id, reason, keep=False
        )

    return ignore_chunk


@vault_agent_tool("enough_information")
def make_enough_information(session: AgentSearchSession) -> Any:
    @strands_tool
    def enough_information(reason: str = "") -> str:
        """Declare that the chunks you have kept would let you answer the
        question well, and end the search. This is how a run should end:
        call it as soon as that is true, however many chunks that took.
        Call it also when repeated searches find nothing new and the
        question still cannot be fully answered, saying what is missing.

        Args:
            reason: One short phrase saying what the kept chunks cover,
                or what is still missing.
        """
        return guard(session, "enough_information") or session.declare_enough(reason)

    return enough_information
