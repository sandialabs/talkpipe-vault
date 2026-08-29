"""Agent-driven retrieval for the Search and Ask pages.

Instead of showing (or answering from) the top-k vector hits, an agent
(talkpipe-agents on top of Strands) searches the vault iteratively and decides
chunk by chunk what is worth having. ``vaultAgentSearch`` shows the chunks it
kept; ``vaultAgentChat`` answers the question from them. It has five tools,
all bound to one ``AgentSearchSession`` per query:

- ``vector_search`` and ``keyword_search`` — the vault's semantic and Whoosh
  searches, callable any number of times with any query, so the agent can
  refine its searches with terms it learns from earlier hits;
- ``extract_keywords`` — asks an LLM to distill a question or phrase into a
  keyword query for the full-text search engine, so the agent does not have
  to guess Whoosh syntax itself (the same ``extractSearchKeywords`` the Ask
  page's keyword boost uses);
- ``keep_chunk`` / ``ignore_chunk`` — the decisions. Kept chunks, in the
  order they were kept, are the results (and, for Ask, the RAG context).

The loop runs for at most ``max_iterations`` agent turns or until the number
of kept chunks reaches ``target_count`` (both from the Settings page); once
the target is reached, further tool calls are cancelled so the model stops.
Every tool call and its outcome is reported through ``progress`` (stdout by
default, so the agent's behavior can be watched from the server console) and
collected in the outcome's ``trace`` for the results page.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any

from strands import Agent
from strands import tool as strands_tool
from strands.hooks import (
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from talkpipe.chatterlang import register_segment
from talkpipe.llm.chat import LLMPrompt
from talkpipe.pipe.basic import AbstractFieldSegment
from talkpipe.pipelines.basic_rag import AppendRAGSources, ConstructRAGPrompt
from talkpipe.search.abstract import SearchResult
from talkpipe.util.config import parse_key_value_str
from talkpipe_agents import build_agent_model

from .config import (
    RAG_PROMPT_DIRECTIVE,
    RAG_SYSTEM_PROMPT,
    get_chat_model,
    get_chat_source,
    get_whoosh_index_path,
)
from .searching_and_prompting import (
    SEARCH_RESULT_RENAMES,
    ExtractSearchKeywords,
    VaultSearch,
    VaultTextSearch,
    normalize_search_result,
)

logger = logging.getLogger(__name__)

DEFAULT_TARGET_COUNT = 5
DEFAULT_MAX_ITERATIONS = 8
# Hits returned per search call, and how much of each chunk the agent sees.
DEFAULT_SEARCH_LIMIT = 8
# Terms in a keyword query built by the extract_keywords tool.
DEFAULT_MAX_KEYWORDS = 8
CHUNK_PREVIEW_CHARS = 500
_ARGS_CHARS = 160

SearchFn = Callable[[str], Iterable[Any]]

SYSTEM_PROMPT = """You select the text chunks from a personal document vault that best answer a user's question. You do not answer the question yourself; you only decide which chunks the user should see.

Work iteratively:
1. Search with vector_search (natural-language, conceptual queries) and keyword_search (exact terms, names, identifiers; Whoosh syntax: terms are ANDed, use OR for alternatives and double quotes for phrases).
2. Read the chunks each search returns. Call keep_chunk only for a chunk that actually contains information answering the question, and ignore_chunk for every other chunk, so that each chunk you have seen carries a decision. Be strict: a chunk that merely shares words with the question is not relevant, and it is better to keep fewer chunks than to keep a wrong one. If a search returned nothing useful, ignore all of its chunks and search differently.
3. Refine: use what you learned from useful chunks - specific terms, names, phrasings, related topics - to run new, more precise searches, and change your keyword or vector queries whenever a search turned up little of use. Do not repeat a query you already ran.
Use extract_keywords when you want a keyword query built for you: give it the question or a topic in plain language and it returns a keyword query to pass to keyword_search. You may also write keyword queries yourself.
Stop as soon as you have kept the target number of chunks, or when further searching is unlikely to find anything new. When you stop, reply with the single word: done."""


@dataclass
class AgentSearchOutcome:
    """What an agent-driven search produced: the kept chunks (as
    SearchResult, in keep order), a one-line note describing how the run
    ended, and the tool-call trace."""

    results: list[SearchResult]
    note: str
    trace: list[str] = field(default_factory=list)


@dataclass
class AgentChatOutcome(AgentSearchOutcome):
    """An agent-retrieved answer. Everything an AgentSearchOutcome carries —
    the kept chunks are the citations and the RAG context the answer was
    built from — plus the answer itself."""

    response: str = ""


@dataclass
class _Chunk:
    id: str
    result: SearchResult
    key: str


class AgentSearchSession:
    """One query's worth of agent state: the chunks discovered so far, the
    keep/ignore decisions, and the tools bound to them.

    The search callables are injected so the session can be exercised without
    a vault or an LLM: ``vector_search``/``keyword_search`` take a query and
    return search hits (SearchResult objects or dicts with
    doc_id/score/document); ``keyword_index_exists`` says whether keyword
    search can run at all; ``extract_keywords`` asks an LLM to turn a
    question into a keyword query (None when no model is available).
    """

    def __init__(
        self,
        *,
        vector_search: SearchFn,
        keyword_search: SearchFn,
        keyword_index_exists: Callable[[], bool],
        extract_keywords: Callable[[str], str] | None,
        target_count: int,
        tell: Callable[[str], None],
        rename_fields: dict[str, str] | None = None,
    ) -> None:
        self._vector_search = vector_search
        self._keyword_search = keyword_search
        self._keyword_index_exists = keyword_index_exists
        self._extract_keywords = extract_keywords
        self.target_count = max(1, target_count)
        self._tell = tell
        self._rename_fields = rename_fields or {}
        self._chunks: dict[str, _Chunk] = {}
        self._by_key: dict[str, str] = {}
        self.kept: list[str] = []
        self.ignored: set[str] = set()
        self.trace: list[str] = []
        self.queries_run: list[tuple[str, str]] = []

    # -- bookkeeping ---------------------------------------------------------

    def tell(self, message: str) -> None:
        self.trace.append(message)
        self._tell(message)

    @property
    def target_reached(self) -> bool:
        return len(self.kept) >= self.target_count

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
        return f"Kept {len(self.kept)} of {self.target_count} target chunks."

    def _run_search(self, kind: str, query: str, search: SearchFn) -> str:
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
        new = 0
        for hit in hits:
            chunk = self._register(hit)
            if chunk is None:
                continue
            if chunk.id not in {c.id for c in chunks}:
                if chunk.id not in self.kept and chunk.id not in self.ignored:
                    new += 1
                chunks.append(chunk)
        self.tell(
            f"agent ← {kind}_search: {len(chunks)} hit(s), {new} undecided. "
            + self._status_line()
        )
        if not chunks:
            return f"No {kind} results for {query!r}. Try different terms."
        body = "\n\n".join(self._describe(c) for c in chunks)
        return (
            f"{len(chunks)} result(s) for {query!r}. Decide on each undecided "
            f"chunk with keep_chunk or ignore_chunk. {self._status_line()}\n\n{body}"
        )

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

    def _decide(self, chunk_id: str, reason: str, keep: bool) -> str:
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
        if self.target_reached:
            return (
                f"{verb.capitalize()} {chunk_id}. Target reached: "
                f"{len(self.kept)} chunks kept. Stop calling tools and reply: done"
            )
        return f"{verb.capitalize()} {chunk_id}. {self._status_line()}"

    # -- tools ---------------------------------------------------------------

    def tools(self) -> list[Any]:
        """The five Strands tools, bound to this session."""
        session = self

        @strands_tool
        def vector_search(query: str) -> str:
            """Semantic search over the vault: finds chunks whose meaning is
            close to the query, even when the words differ. Best with a
            full question or descriptive phrase. Each result carries a chunk
            id to pass to keep_chunk or ignore_chunk.

            Args:
                query: A natural-language question or descriptive phrase.
            """
            return session._run_search("vector", query, session._vector_search)

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
            if not session._keyword_index_exists():
                session.tell("agent ← keyword_search: no keyword index exists")
                return (
                    "This vault has no full-text index, so keyword search is "
                    "unavailable; use vector_search instead."
                )
            return session._run_search("keyword", query, session._keyword_search)

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
            return session.extract_keywords(question)

        @strands_tool
        def keep_chunk(chunk_id: str, reason: str = "") -> str:
            """Keep a chunk: it is relevant to the question and the user
            should see it. Kept chunks count toward the target.

            Args:
                chunk_id: The id shown with the chunk, e.g. c3.
                reason: One short phrase saying why it is relevant.
            """
            return session._decide(chunk_id, reason, keep=True)

        @strands_tool
        def ignore_chunk(chunk_id: str, reason: str = "") -> str:
            """Ignore a chunk: it is not relevant to the question and must
            not be shown to the user.

            Args:
                chunk_id: The id shown with the chunk, e.g. c3.
                reason: One short phrase saying why it is irrelevant.
            """
            return session._decide(chunk_id, reason, keep=False)

        return [
            vector_search,
            keyword_search,
            extract_keywords,
            keep_chunk,
            ignore_chunk,
        ]


class _SessionHooks(HookProvider):
    """Report every tool call before it runs and cancel any call made after
    the target is reached, so the agent stops instead of over-collecting."""

    def __init__(self, session: AgentSearchSession) -> None:
        self._session = session

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use.get("name")
        args = event.tool_use.get("input", {})
        shown = ", ".join(f"{k}={v!r}" for k, v in dict(args).items())
        if len(shown) > _ARGS_CHARS:
            shown = shown[:_ARGS_CHARS] + "…"
        if self._session.target_reached:
            self._session.tell(f"agent ✗ {name}({shown}) cancelled: target reached")
            event.cancel_tool = (
                f"Target of {self._session.target_count} kept chunks already "
                "reached. Stop calling tools and reply: done"
            )
            return
        self._session.tell(f"agent → {name}({shown})")

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        if event.exception is not None:
            self._session.tell(
                f"agent ← {event.tool_use.get('name')} raised: {event.exception}"
            )


def _print_progress(message: str) -> None:
    print(message, flush=True)


@register_segment("vaultAgentSearch")
class AgentVaultSearch(AbstractFieldSegment):
    """
    Segment that lets an agent select a vault's search results iteratively.

    Expects input items containing a search query string (either as the full
    item or in a specified field). For each query an agent runs the vault's
    vector and keyword searches as tools — with an LLM keyword extractor it
    can call to build a keyword query — refining its queries from what it
    finds, and keeps or ignores every chunk it sees until it has kept the
    target number of chunks or the iteration cap is hit.

    Emits an AgentSearchOutcome: the kept chunks as SearchResult objects (in
    keep order), a note describing how the run ended, and the trace of tool
    calls that was also reported through the progress callable.
    """

    def __init__(
        self,
        vault_path: Annotated[str, "Path to LanceDB created by makevectordatabase"],
        field: Annotated[
            str | None, "The field to extract. If none, use full item."
        ] = None,
        set_as: Annotated[str | None, "The field to set/append the result as."] = None,
        multi_emit: Annotated[
            bool,
            "Whether this class potentially emits multiple results per item."
            "Should be set by the subclass constructor call or the field_segment decorator, not by the user.",
        ] = False,
        embedding_model: Annotated[
            str | None,
            "Model name for generating embeddings. If None, uses TalkPipe config or default.",
        ] = None,
        embedding_source: Annotated[
            str | None,
            "Source/provider for the embedding model. If None, uses TalkPipe config or default.",
        ] = None,
        chat_model: Annotated[
            str | None,
            "Model the agent reasons with. If None, uses TalkPipe config or default.",
        ] = None,
        chat_source: Annotated[
            str | None,
            "Source/provider of the agent's model. If None, uses TalkPipe config or default.",
        ] = None,
        target_count: Annotated[
            int, "Stop once this many chunks have been kept."
        ] = DEFAULT_TARGET_COUNT,
        max_iterations: Annotated[
            int, "Cap on agent turns per query."
        ] = DEFAULT_MAX_ITERATIONS,
        search_limit: Annotated[
            int, "Hits returned to the agent per search call."
        ] = DEFAULT_SEARCH_LIMIT,
        rename_fields: Annotated[
            str | None,
            "Document fields to rename during normalization, as 'old:new,old:new'.",
        ] = SEARCH_RESULT_RENAMES,
        max_keywords: Annotated[
            int, "Maximum terms in a query built by the extract_keywords tool."
        ] = DEFAULT_MAX_KEYWORDS,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.vault_path = vault_path
        self.chat_model = chat_model if chat_model is not None else get_chat_model()
        self.chat_source = chat_source if chat_source is not None else get_chat_source()
        self.target_count = max(1, target_count)
        self.max_iterations = max(1, max_iterations)
        self.search_limit = max(1, search_limit)
        self.rename_fields = (
            parse_key_value_str(rename_fields, require_value=True)
            if rename_fields
            else {}
        )
        self.tell = progress or _print_progress
        self._vector_search = VaultSearch(
            vault_path=vault_path,
            embedding_model=embedding_model,
            embedding_source=embedding_source,
            limit=self.search_limit,
        ).as_function(single_in=True, single_out=True)
        # Same LLM keyword distillation the Ask page's keyword boost uses.
        self._extract_keywords = ExtractSearchKeywords(
            chat_model=self.chat_model,
            chat_source=self.chat_source,
            max_keywords=max(1, max_keywords),
        ).as_function(single_in=True, single_out=True)
        # Fail on a bad model configuration when the segment is built, not
        # mid-search, so the app can fall back to plain search up front.
        self._model = build_agent_model(self.chat_source, self.chat_model)

    # -- search bindings (overridable for tests) -----------------------------

    def vector_search(self, query: str) -> list[Any]:
        raw = self._vector_search(query)
        if isinstance(raw, dict):
            raw = [raw]
        return list(raw or [])

    def keyword_index_exists(self) -> bool:
        return os.path.isdir(get_whoosh_index_path(self.vault_path))

    def keyword_search(self, query: str) -> list[Any]:
        # Built per call: the index may not have existed when the segment was
        # constructed (the agent can create it mid-run).
        search = VaultTextSearch(vault_path=self.vault_path, limit=self.search_limit)
        return list(search.process_value(query))

    def extract_keywords(self, question: str) -> str:
        return str(self._extract_keywords(question))

    def make_session(self) -> AgentSearchSession:
        return AgentSearchSession(
            vector_search=self.vector_search,
            keyword_search=self.keyword_search,
            keyword_index_exists=self.keyword_index_exists,
            extract_keywords=self.extract_keywords,
            target_count=self.target_count,
            tell=self.tell,
            rename_fields=self.rename_fields,
        )

    def run_agent(self, session: AgentSearchSession, query: str) -> str:
        """Drive the agent over *session* for *query*; returns the stop reason."""
        agent = Agent(
            model=self._model,
            tools=session.tools(),
            system_prompt=SYSTEM_PROMPT,
            # the default handler prints to stdout — never in a pipeline
            callback_handler=None,
            hooks=[_SessionHooks(session)],
        )
        prompt = (
            f"Question: {query}\n"
            f"Target: keep {session.target_count} relevant chunks. "
            "Nothing has been searched yet."
        )
        result = agent(prompt, limits={"turns": self.max_iterations})
        return str(result.stop_reason)

    def select_chunks(self, value: str) -> AgentSearchOutcome:
        """Run one agent-driven selection for *value* and report the outcome.

        Kept separate from ``process_value`` so the Ask variant below — and
        the app, which builds one segment and drives both pages from it — can
        reuse the selection without the segment's emission behavior.
        """
        session = self.make_session()
        session.tell(
            f"agent search: {value!r} (target {session.target_count}, "
            f"max {self.max_iterations} iterations)"
        )
        try:
            stop_reason = self.run_agent(session, value)
        except Exception as exc:
            logger.warning("Agent search failed", exc_info=True)
            session.tell(f"agent search failed: {exc}")
            note = f"The agent stopped with an error ({exc}); showing what it kept."
            return AgentSearchOutcome(session.results, note, session.trace)
        kept = len(session.kept)
        searches = len(session.queries_run)
        if session.target_reached:
            how = "reached its target"
        elif stop_reason.startswith("limit"):
            how = f"hit the {self.max_iterations}-iteration cap"
        else:
            how = "stopped on its own"
        note = (
            f"Agent {how}: kept {kept} of {session.target_count} chunks after "
            f"{searches} search{'es' if searches != 1 else ''}, "
            f"ignored {len(session.ignored)}."
        )
        session.tell(note)
        return AgentSearchOutcome(session.results, note, session.trace)

    def process_value(self, value: str) -> AgentSearchOutcome:
        return self.select_chunks(value)


# Shown instead of an answer when the agent kept nothing: the RAG prompt would
# have no context at all, and a model asked to answer from nothing invents one.
NO_CONTEXT_ANSWER = (
    "The agent did not find any chunk in this vault that answers the "
    "question, so there was nothing to answer from. Try rephrasing the "
    "question, indexing more documents, or asking without the agent."
)


@register_segment("vaultAgentChat")
class AgentVaultChat(AgentVaultSearch):
    """
    Segment that answers a question from chunks an agent selected.

    Expects input items containing a question string (either as the full item
    or in a specified field). The agent gathers the context exactly as
    vaultAgentSearch does — iterative vector and keyword search with a
    keep/ignore decision on every chunk it sees — and the chunks it kept
    become the background of a RAG prompt answered by the chat model, with
    the cited sources appended. When it kept nothing, no prompt is sent.

    Emits an AgentChatOutcome: the answer, the kept chunks as SearchResult
    objects (in keep order), a note describing how the selection ended, and
    the trace of tool calls that was also reported through the progress
    callable.
    """

    def __init__(
        self,
        vault_path: Annotated[str, "Path to LanceDB created by makevectordatabase"],
        field: Annotated[
            str | None, "The field to extract. If none, use full item."
        ] = None,
        set_as: Annotated[str | None, "The field to set/append the result as."] = None,
        multi_emit: Annotated[
            bool,
            "Whether this class potentially emits multiple results per item."
            "Should be set by the subclass constructor call or the field_segment decorator, not by the user.",
        ] = False,
        embedding_model: Annotated[
            str | None,
            "Model name for generating embeddings. If None, uses TalkPipe config or default.",
        ] = None,
        embedding_source: Annotated[
            str | None,
            "Source/provider for the embedding model. If None, uses TalkPipe config or default.",
        ] = None,
        chat_model: Annotated[
            str | None,
            "Model the agent reasons and answers with. If None, uses TalkPipe config or default.",
        ] = None,
        chat_source: Annotated[
            str | None,
            "Source/provider of the agent's model. If None, uses TalkPipe config or default.",
        ] = None,
        target_count: Annotated[
            int, "Stop once this many chunks have been kept."
        ] = DEFAULT_TARGET_COUNT,
        max_iterations: Annotated[
            int, "Cap on agent turns per question."
        ] = DEFAULT_MAX_ITERATIONS,
        search_limit: Annotated[
            int, "Hits returned to the agent per search call."
        ] = DEFAULT_SEARCH_LIMIT,
        rename_fields: Annotated[
            str | None,
            "Document fields to rename during normalization, as 'old:new,old:new'.",
        ] = SEARCH_RESULT_RENAMES,
        max_keywords: Annotated[
            int, "Maximum terms in a query built by the extract_keywords tool."
        ] = DEFAULT_MAX_KEYWORDS,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            vault_path=vault_path,
            field=field,
            set_as=set_as,
            multi_emit=multi_emit,
            embedding_model=embedding_model,
            embedding_source=embedding_source,
            chat_model=chat_model,
            chat_source=chat_source,
            target_count=target_count,
            max_iterations=max_iterations,
            search_limit=search_limit,
            rename_fields=rename_fields,
            max_keywords=max_keywords,
            progress=progress,
        )
        # The same RAG prompt -> completion -> sources stages vaultChat ends
        # with, so an agent-gathered answer is worded and cited identically;
        # only the retrieval in front of them differs.
        self._answer = (
            ConstructRAGPrompt(
                prompt_directive=RAG_PROMPT_DIRECTIVE,
                background_field="_background",
                content_field="query",
                set_as="_ragprompt",
            )
            | LLMPrompt(
                model=self.chat_model,
                source=self.chat_source,
                system_prompt=RAG_SYSTEM_PROMPT,
                field="_ragprompt",
                set_as="_partial_rag_response",
            )
            | AppendRAGSources(
                partial_answer_field="_partial_rag_response",
                set_as="chat_response",
            )
        ).as_function(single_in=True, single_out=True)

    def answer(self, question: str, background: list[SearchResult]) -> str:
        """Answer *question* from *background* with the RAG prompt."""
        result = self._answer({"query": question, "_background": background})
        return str(result.get("chat_response", ""))

    def process_value(self, value: str) -> AgentChatOutcome:
        outcome = self.select_chunks(value)
        if not outcome.results:
            return AgentChatOutcome(
                outcome.results, outcome.note, outcome.trace, NO_CONTEXT_ANSWER
            )
        return AgentChatOutcome(
            outcome.results,
            outcome.note,
            outcome.trace,
            self.answer(value, outcome.results),
        )
