"""Agent-driven retrieval for the Search and Ask pages.

Instead of showing (or answering from) the top-k vector hits, an agent
searches the vault iteratively and decides chunk by chunk what is worth
having. ``vaultAgentSearch`` shows the chunks it kept; ``vaultAgentChat``
answers the question from them.

**How it is put together.** The agent itself is talkpipe-agents' ``llmAgent``
segment, driven with a tool list resolved by name from ``agent_tools``. The
segments here own the *vault* concerns — opening the vault, wiring the
searches the tools call, running the RAG prompt over what the agent kept —
and nothing about running a model. Which tools an agent gets is a parameter,
and so is its system prompt, so tools and agents mix and match without
editing this file::

    vaultAgentChat[vault_path="~/notes-vault",
                   tools="vector_search,keep_chunk,ignore_chunk,enough_information,think"]

Names the vault does not own (``think`` above) come from talkpipe-agents'
own registry. What each tool does lives in ``agent_tools``; see its
docstring, and ``docs/AGENT_RETRIEVAL.md`` for using the tools elsewhere.

**The stopping criterion is the agent's judgement, not a result count.** It
searches, keeps what the answer needs, and asks itself after each search what
the question still lacks; it stops when it can answer, whether that took one
chunk or eight. ``max_chunks`` (Settings → Agent Chunk Limit) is a ceiling on
how much context one run may collect, not a goal, and ``max_iterations``
(Settings → Agent Search Iterations) caps the agent turns — both exist only
to bound a run that never reaches the judgement. The outcome's note says
which of the four endings happened, because each means something different
about how complete the selection is.

Every tool call and its outcome is reported through ``progress`` (stdout by
default, so the agent's behavior can be watched from the server console) and
collected in the outcome's ``trace`` for the results page.
"""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Annotated, Any, Self

from talkpipe.chatterlang import register_segment
from talkpipe.data.extraction import ReadFile
from talkpipe.llm.chat import LLMPrompt
from talkpipe.pipe.basic import AbstractFieldSegment
from talkpipe.pipelines.basic_rag import AppendRAGSources, ConstructRAGPrompt
from talkpipe.search.abstract import SearchResult
from talkpipe.util.config import parse_key_value_str
from talkpipe_agents import LLMAgent, build_agent_model

from .agent_tools import AgentSearchSession, build_tools, parse_tool_names
from .config import (
    RAG_PROMPT_DIRECTIVE,
    RAG_SYSTEM_PROMPT,
    get_chat_model,
    get_chat_source,
    get_vector_db_path,
    get_whoosh_index_path,
)
from .searching_and_prompting import (
    SEARCH_RESULT_RENAMES,
    ExtractSearchKeywords,
    VaultSearch,
    VaultTextSearch,
    resolve_vault_paths,
)

logger = logging.getLogger(__name__)

# A ceiling on how much context one run may collect, not a goal: the agent
# stops when it judges it can answer the question well, and this only bounds
# a run that never reaches that judgement.
DEFAULT_MAX_CHUNKS = 10
DEFAULT_MAX_ITERATIONS = 8
# Hits returned per search call, and how much of each chunk the agent sees.
DEFAULT_SEARCH_LIMIT = 8
# Terms in a keyword query built by the extract_keywords tool.
DEFAULT_MAX_KEYWORDS = 8

RETRIEVAL_SYSTEM_PROMPT = """You gather the text chunks from a personal document vault that are needed to answer a user's question well. You do not answer the question yourself; you decide which chunks the answer will be built from.

Work iteratively:
1. Search with vector_search (natural-language, conceptual queries) and keyword_search (exact terms, names, identifiers; Whoosh syntax: terms are ANDed, use OR for alternatives and double quotes for phrases).
2. Read the chunks each search returns and decide on every one with keep_chunk or ignore_chunk, so that each chunk you have seen carries a decision. Keep a chunk that is about what the question asks and holds something the answer could use - a fact, a step, a setting, an example, a definition, background, or one part of the answer even if not all of it. Ignore a chunk that is about something else; one that merely shares words with the question is not relevant. When you are unsure, keep it: the answer is written from the kept chunks alone, so an ignored chunk is lost to it, while a kept chunk that adds little costs little. A chunk shown cut off (ending in …) continues beyond what you see, so judge it by what it is about. If a search returned nothing on the question's topic, ignore its chunks and search differently.
3. After each search, ask yourself what the question still needs. Could you answer it completely and accurately from the chunks you have kept so far? If some part is still missing - a fact, a date, a name, the other side of a comparison, a second document that should corroborate the first - search for that specific missing piece, using terms, names and phrasings you learned from the chunks you kept. Do not repeat a query you already ran.
4. Stop by calling enough_information as soon as the chunks you have kept would let you answer the question well, saying in one short phrase what they cover. That judgement is the reason to stop - not the number of chunks you have collected. Keeping more chunks that add nothing is not better than stopping. If repeated searches keep turning up nothing new and the question still cannot be fully answered, call enough_information anyway and say what is missing.
Use extract_keywords when you want a keyword query built for you: give it the question or a topic in plain language and it returns a keyword query to pass to keyword_search. You may also write keyword queries yourself.
Use read_document when a chunk suggests its document holds more of the answer - the chunk is cut off, it is one step of a procedure or one item of a list, or its document is plainly where the question's topic is covered. It returns the document's text as a new chunk to keep or ignore; reading is not keeping.
After calling enough_information, or once you are told to stop, reply with the single word: done."""

# Shown instead of an answer when the agent kept nothing: the RAG prompt would
# have no context at all, and a model asked to answer from nothing invents one.
# It leads with what that most often means for the reader -- the notes do not
# cover the question -- before the ways to check that it is not the agent.
NO_CONTEXT_ANSWER = (
    "The notes in this vault don't appear to answer this question: the agent "
    "searched them and found nothing relevant. If you expected an answer, try "
    "rephrasing the question, indexing the documents that hold it, choosing a "
    "chat model with stronger tool calling, or asking without the agent."
)

# Shown instead of an answer when the run failed before keeping anything: the
# agent never got to look, so the vault and the question are not the problem.
AGENT_ERROR_ANSWER = (
    "The agent stopped with an error before it kept any chunk, so there was "
    "nothing to answer from; the note says what went wrong. Check that the "
    "chat model is available and supports tool calling, or ask without the "
    "agent."
)

# Shown instead of an answer when the agent kept nothing but left chunks it
# had found undecided: it never judged them, so saying the notes do not cover
# the question would be wrong. Some models spend every iteration searching
# and reading without deciding (minimax-m3.1-flash on Ollama, whose reasoning
# is not carried from one turn to the next).
UNDECIDED_ANSWER = (
    "The agent found passages in this vault but stopped before keeping any "
    "of them, so there was nothing to answer from; its tool calls show what "
    "it searched. That usually comes from the chat model, not the notes: some "
    "models spend every iteration searching and reading without deciding. Try "
    "another chat model, or ask without the agent."
)

# Appended to the note of a run that ended without the agent's judgement, the
# ceiling, or the turn cap: that is how a model weak at tool calling ends a
# run, and nothing else on the page would tell the reader the model is why.
WEAK_TOOL_CALLING_HINT = (
    "Models weak at tool calling usually end a run this way; a chat model "
    "with stronger tool calling may do better."
)


@dataclass
class AgentSearchOutcome:
    """What an agent-driven search produced: the kept chunks (as
    SearchResult, in keep order), a one-line note describing how the run
    ended, and the tool-call trace."""

    results: list[SearchResult]
    note: str
    trace: list[str] = field(default_factory=list)
    # The exception text when the run stopped with an error, else "".
    # Keyword-only, so AgentChatOutcome's positional fields are unchanged.
    error: str = field(default="", kw_only=True)
    # How many chunks the agent was shown but neither kept nor ignored.
    undecided: int = field(default=0, kw_only=True)


@dataclass
class AgentChatOutcome(AgentSearchOutcome):
    """An agent-retrieved answer. Everything an AgentSearchOutcome carries —
    the kept chunks are the citations and the RAG context the answer was
    built from — plus the answer itself."""

    response: str = ""


# Reads a document for the read_document tool through the registry indexing
# reads with, so whatever could be indexed can be read back whole.
_read_file = ReadFile(skip_errors=False).as_function(single_in=True)


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
    finds, and keeps or ignores every chunk it sees. It runs until it judges
    that what it has kept would answer the question well and says so, or
    until the chunk ceiling or the iteration cap stops it.

    Emits an AgentSearchOutcome: the kept chunks as SearchResult objects (in
    keep order), a note describing how the run ended, and the trace of tool
    calls that was also reported through the progress callable.
    """

    def __init__(
        self,
        vault_path: Annotated[
            str | list[str],
            "Vault path (LanceDB created by makevectordatabase), or a folder "
            "with a folder-mode index; a list searches them together",
        ],
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
            "Model the agent reasons (and, for vaultAgentChat, answers) with. If None, uses TalkPipe config or default.",
        ] = None,
        chat_source: Annotated[
            str | None,
            "Source/provider of the agent's model. If None, uses TalkPipe config or default.",
        ] = None,
        tools: Annotated[
            str | list[str] | None,
            "Tools the agent gets, by name ('vector_search,keep_chunk,…' or a "
            "list). Vault tools come from agent_tools; any other name is "
            "resolved in talkpipe-agents' registry. None = the whole vault pack.",
        ] = None,
        system_prompt: Annotated[
            str | None, "The agent's system prompt. If None, uses the retrieval prompt."
        ] = None,
        max_chunks: Annotated[
            int,
            "Ceiling on chunks kept per run. Not a target: the agent stops "
            "when it judges it can answer the question, and this only bounds "
            "a run that never gets there.",
        ] = DEFAULT_MAX_CHUNKS,
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
        progress: Annotated[
            Callable[[str], None] | None,
            "Called with each trace line as the agent works (tool calls, "
            "decisions, how the run ended). None prints them to stdout; pass "
            "a logger method, a list's append, or a no-op to keep them off it.",
        ] = None,
    ) -> None:
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.vault_path = vault_path
        self.embedding_model = embedding_model
        self.embedding_source = embedding_source
        self.chat_model = chat_model if chat_model is not None else get_chat_model()
        self.chat_source = chat_source if chat_source is not None else get_chat_source()
        self.tool_names = parse_tool_names(tools)
        self.system_prompt = system_prompt or RETRIEVAL_SYSTEM_PROMPT
        self.max_chunks = max(1, max_chunks)
        self.max_iterations = max(1, max_iterations)
        self.search_limit = max(1, search_limit)
        self.max_keywords = max(1, max_keywords)
        self.rename_fields = (
            parse_key_value_str(rename_fields, require_value=True)
            if rename_fields
            else {}
        )
        self.tell = progress or _print_progress
        # Resolve the tool names when the segment is built, not mid-search, so
        # an unknown tool surfaces up front (compile time for ChatterLang).
        build_tools(self.make_session(), self.tool_names)
        self._bind_vault()

    def _bind_vault(self) -> None:
        """Open the vault and the models the search bindings below rely on.

        The one step of construction that touches the outside world — the
        vault on disk, the embedder, the keyword-extraction model, the agent
        model — so a test double can replace just this and run the real
        constructor otherwise. The agent model is resolved here rather than
        per query so a bad chat configuration fails the build, where the app
        can fall back to plain search, instead of failing mid-search.
        A vault path that does not exist is refused here too: searching it
        would create an empty folder and send the agent through a whole run
        of empty searches before anyone saw the typo.
        """
        # A folder stands for its folder-mode index; resolved again at each
        # search, so a folder indexed after this segment was built is found.
        for path in resolve_vault_paths(self.vault_path):
            if not os.path.isdir(get_vector_db_path(path)):
                raise ValueError(
                    f"No vault at {path!r}: the folder does not exist. "
                    "Check the path, or build the vault first (makevectordatabase "
                    "or the Vaults & Documents page)."
                )
        build_agent_model(self.chat_source, self.chat_model)
        self._vector_search = VaultSearch(
            vault_path=self.vault_path,
            embedding_model=self.embedding_model,
            embedding_source=self.embedding_source,
            limit=self.search_limit,
        ).as_function(single_in=True, single_out=True)
        # Same LLM keyword distillation the Ask page's keyword boost uses.
        self._extract_keywords = ExtractSearchKeywords(
            chat_model=self.chat_model,
            chat_source=self.chat_source,
            max_keywords=self.max_keywords,
        ).as_function(single_in=True, single_out=True)

    def with_max_iterations(self, max_iterations: int) -> Self:
        """A copy of this segment that may take *max_iterations* turns.

        The copy shares the vault, embedder and model bindings, which hold no
        per-query state, so the app can offer a longer run (Extra Effort)
        next to the normal one without opening the vault twice.
        """
        clone = copy.copy(self)
        clone.max_iterations = max(1, max_iterations)
        return clone

    # -- search bindings (overridable for tests) -----------------------------

    def vector_search(self, query: str) -> list[Any]:
        raw = self._vector_search(query)
        if isinstance(raw, dict):
            raw = [raw]
        return list(raw or [])

    def keyword_index_exists(self) -> bool:
        return any(
            os.path.isdir(get_whoosh_index_path(path))
            for path in resolve_vault_paths(self.vault_path)
        )

    def keyword_search(self, query: str) -> list[Any]:
        # Built per call: the index may not have existed when the segment was
        # constructed (it can be built between two questions).
        search = VaultTextSearch(vault_path=self.vault_path, limit=self.search_limit)
        return list(search.process_value(query))

    def extract_keywords(self, question: str) -> str:
        return str(self._extract_keywords(question))

    def read_document(self, source: str) -> str:
        # The source comes from a hit the vault returned, never from the
        # agent, so only documents this vault indexed can be read — with the
        # same extractors indexing used.
        if not os.path.isfile(source):
            raise FileNotFoundError(
                f"{os.path.basename(source)} is not on this server's filesystem"
            )
        return "\n".join(item.content for item in _read_file(source))

    def make_session(self) -> AgentSearchSession:
        return AgentSearchSession(
            vector_search=self.vector_search,
            keyword_search=self.keyword_search,
            keyword_index_exists=self.keyword_index_exists,
            extract_keywords=self.extract_keywords,
            max_chunks=self.max_chunks,
            tell=self.tell,
            rename_fields=self.rename_fields,
            read_document=self.read_document,
        )

    def make_agent(self, session: AgentSearchSession) -> LLMAgent:
        """The llmAgent for one query, with this segment's tools bound to
        *session*. Built per query because the tools carry that query's
        state; the model behind it is cheap to resolve."""
        return LLMAgent(
            model=self.chat_model,
            source=self.chat_source,
            system_prompt=self.system_prompt,
            tools=build_tools(session, self.tool_names),
            max_turns=self.max_iterations,
            progress=session.tell,
        )

    def run_agent(self, session: AgentSearchSession, query: str) -> str:
        """Drive the agent over *session* for *query*; returns the stop reason."""
        agent = self.make_agent(session)
        prompt = (
            f"Question: {query}\n"
            "Nothing has been searched yet. Search, keep the chunks that help "
            "answer it, and call enough_information once they would answer this "
            f"question well. You may keep at most {session.max_chunks} chunks."
        )
        agent.as_function(single_in=True, single_out=True)(prompt)
        # The library keeps the Strands result of the last item; its stop
        # reason tells a run the turn cap ended from one the agent ended.
        return str(agent.last_result.stop_reason) if agent.last_result else ""

    def select_chunks(self, value: str) -> AgentSearchOutcome:
        """Run one agent-driven selection for *value* and report the outcome.

        Kept separate from ``process_value`` so the Ask variant below — and
        the app, which builds one segment and drives both pages from it — can
        reuse the selection without the segment's emission behavior.
        """
        session = self.make_session()
        session.tell(
            f"agent search: {value!r} (at most {session.max_chunks} chunks, "
            f"{self.max_iterations} iterations)"
        )
        try:
            stop_reason = self.run_agent(session, value)
        except Exception as exc:
            logger.warning("Agent search failed", exc_info=True)
            session.tell(f"agent search failed: {exc}")
            note = f"The agent stopped with an error ({exc}); " + (
                "showing what it kept." if session.kept else "it kept nothing."
            )
            return AgentSearchOutcome(
                session.results,
                note,
                session.trace,
                error=str(exc) or repr(exc),
                undecided=len(session.undecided),
            )
        kept = len(session.kept)
        undecided = len(session.undecided)
        searches = len(session.queries_run)
        # The four ways a run can end, most to least satisfying. The first is
        # the intended one; the rest are worth telling apart, because each
        # means the selection may be incomplete for a different reason.
        hint = ""
        if session.enough:
            how = "judged it had enough to answer"
            if session.enough_reason:
                how += f" ({session.enough_reason})"
        elif session.at_capacity:
            how = f"filled its {session.max_chunks}-chunk maximum"
        elif stop_reason.startswith("limit"):
            how = f"hit the {self.max_iterations}-iteration cap"
        else:
            how = "stopped without saying it had enough"
            hint = f" {WEAK_TOOL_CALLING_HINT}"
        note = (
            f"Agent {how}: kept {kept} chunk{'s' if kept != 1 else ''} after "
            f"{searches} search{'es' if searches != 1 else ''}, "
            f"ignored {len(session.ignored)}"
            + (f", left {undecided} undecided" if undecided else "")
            + f".{hint}"
        )
        session.tell(note)
        return AgentSearchOutcome(
            session.results, note, session.trace, undecided=undecided
        )

    def process_value(self, value: str) -> AgentSearchOutcome:
        return self.select_chunks(value)


@register_segment("vaultAgentChat")
class AgentVaultChat(AgentVaultSearch):
    """
    Segment that answers a question from chunks an agent selected.

    Expects input items containing a question string (either as the full item
    or in a specified field). The agent gathers the context exactly as
    vaultAgentSearch does — iterative vector and keyword search with a
    keep/ignore decision on every chunk it sees, until it judges that what it
    has kept would answer the question well — and the chunks it kept become
    the background of a RAG prompt answered by the chat model, with the cited
    sources appended. When it kept nothing, no prompt is sent.

    Takes the same parameters as vaultAgentSearch. Emits an AgentChatOutcome:
    the answer, the kept chunks as SearchResult objects (in keep order), a
    note describing how the selection ended, and the trace of tool calls that
    was also reported through the progress callable.
    """

    @cached_property
    def _answer(self) -> Callable[[dict[str, Any]], Any]:
        # The same RAG prompt -> completion -> sources stages vaultChat ends
        # with, so an agent-gathered answer is worded and cited identically;
        # only the retrieval in front of them differs. Built on first use:
        # the chat model was already validated when the segment was built.
        return (
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
        if outcome.results:
            response = self.answer(value, outcome.results)
        elif outcome.error:
            response = AGENT_ERROR_ANSWER
        elif outcome.undecided:
            response = UNDECIDED_ANSWER
        else:
            response = NO_CONTEXT_ANSWER
        return AgentChatOutcome(
            outcome.results,
            outcome.note,
            outcome.trace,
            response,
            error=outcome.error,
            undecided=outcome.undecided,
        )
