"""Tests for agent-driven retrieval: the vault's agent tool pack
(pipelines/agent_tools.py) and the segments built on it
(pipelines/agent_search.py).

The session and its tools are exercised with injected search callables and a
scripted stand-in for the agent loop, so nothing here needs a vault, an
embedder, or an LLM.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import pytest
from talkpipe.search.abstract import SearchResult

from talkpipe_vault.pipelines import agent_search, agent_tools
from talkpipe_vault.pipelines.agent_search import AgentSearchOutcome, AgentVaultSearch
from talkpipe_vault.pipelines.agent_tools import AgentSearchSession, build_tools


def _hit(doc_id: str, content: str, **doc: Any) -> SearchResult:
    return SearchResult(score=0.5, doc_id=doc_id, document={"content": content, **doc})


def _tools(session: AgentSearchSession, names: Any = None) -> dict[str, Any]:
    """The session's tools, by tool name (the whole vault pack by default)."""
    resolved = build_tools(session, agent_tools.parse_tool_names(names))
    return {t.tool_name: t for t in resolved}


def _make_session(
    *,
    vector: dict[str, list[Any]] | None = None,
    keyword: dict[str, list[Any]] | None = None,
    index_exists: bool = True,
    extract_keywords: Any = None,
    max_chunks: int = 2,
    read_document: Any = None,
) -> tuple[AgentSearchSession, list[str]]:
    told: list[str] = []
    vector = vector or {}
    keyword = keyword or {}
    session = AgentSearchSession(
        vector_search=lambda q: vector.get(q, []),
        keyword_search=lambda q: keyword.get(q, []),
        keyword_index_exists=lambda: index_exists,
        extract_keywords=extract_keywords,
        max_chunks=max_chunks,
        tell=told.append,
        read_document=read_document,
    )
    return session, told


class TestSessionTools:
    def test_search_assigns_ids_and_dedupes_across_searches(self):
        session, _ = _make_session(
            vector={"q": [_hit("a", "alpha text", title="A"), _hit("b", "beta")]},
            keyword={"alpha": [_hit("a", "alpha text"), _hit("c", "gamma")]},
        )
        tools = _tools(session)

        out = tools["vector_search"](query="q")
        assert "[c1] A\nalpha text" in out
        assert "[c2]\nbeta" in out

        out = tools["keyword_search"](query="alpha")
        # the same doc_id keeps its id; the new one gets the next id
        assert "[c1]" in out
        assert "[c3]\ngamma" in out
        assert "c4" not in out

        # The trace a person reads names the document behind the short id.
        session, told = _make_session(vector={"q": [_hit("a", "x", title="A.md")]})
        tools = _tools(session)
        tools["vector_search"](query="q")
        tools["keep_chunk"](chunk_id="c1", reason="on topic")
        assert any("keep c1 (A.md) — on topic" in line for line in told)

    def test_repeated_query_is_refused(self):
        session, _ = _make_session(vector={"q": [_hit("a", "x")]})
        tools = _tools(session)
        tools["vector_search"](query="q")
        assert "already ran" in tools["vector_search"](query="q")
        assert session.queries_run == [("vector", "q")]

    def test_empty_results_and_failures_are_reported_to_the_agent(self):
        def boom(_query: str) -> list[Any]:
            raise RuntimeError("index unreadable")

        session = AgentSearchSession(
            vector_search=lambda q: [],
            keyword_search=boom,
            keyword_index_exists=lambda: True,
            extract_keywords=None,
            max_chunks=1,
            tell=lambda _m: None,
        )
        tools = _tools(session)
        assert "No vector results" in tools["vector_search"](query="nothing")
        assert "index unreadable" in tools["keyword_search"](query="x")

    def test_keep_and_ignore_drive_results_and_the_chunk_ceiling(self):
        session, told = _make_session(
            vector={"q": [_hit("a", "one"), _hit("b", "two"), _hit("c", "three")]},
            max_chunks=2,
        )
        tools = _tools(session)
        tools["vector_search"](query="q")

        assert "Unknown chunk id" in tools["keep_chunk"](chunk_id="c9")
        out = tools["keep_chunk"](chunk_id="c2", reason="on topic")
        assert "Kept 1 chunk(s), at most 2." in out
        # Every keep points at the real stopping criterion, not at a count.
        assert "enough_information" in out
        assert "already kept" in tools["keep_chunk"](chunk_id="c2")
        tools["ignore_chunk"](chunk_id="c1", reason="off topic")
        assert not session.should_stop

        out = tools["keep_chunk"](chunk_id="c3")
        assert "maximum of 2 chunks" in out
        assert "done" in out
        assert session.at_capacity
        assert session.should_stop
        assert not session.enough  # the ceiling stopped it, not its judgement
        assert [r.doc_id for r in session.results] == ["b", "c"]
        assert session.ignored == {"c1"}
        assert any("keep c2 — on topic" in line for line in told)
        assert any("ignore c1 — off topic" in line for line in told)

    def test_enough_information_ends_the_run_at_any_chunk_count(self):
        """The stopping criterion is the agent's judgement, not a count."""
        session, told = _make_session(
            vector={"q": [_hit("a", "one"), _hit("b", "two")]}, max_chunks=5
        )
        tools = _tools(session)
        tools["vector_search"](query="q")
        tools["keep_chunk"](chunk_id="c1")

        assert not session.should_stop  # one of five kept, but that is not the test
        out = tools["enough_information"](reason="covers the whole question")
        assert "done" in out
        assert session.enough
        assert session.should_stop
        assert session.enough_reason == "covers the whole question"
        assert [r.doc_id for r in session.results] == ["a"]
        assert any("enough information — covers the whole" in line for line in told)

    def test_enough_information_is_refused_with_nothing_kept(self):
        """Stopping with no context produces no answer; keep searching instead."""
        session, told = _make_session(vector={"q": [_hit("a", "one")]}, max_chunks=5)
        tools = _tools(session)
        tools["vector_search"](query="q")

        out = tools["enough_information"](reason="nothing here")
        assert "not kept any chunk" in out
        assert not session.enough
        assert not session.should_stop
        assert "refused" in told[-1]

    def test_an_unknown_chunk_id_is_refused_in_the_trace_too(self):
        """A model that keeps a chunk before any search has returned it (a
        guessed id, or a keep sent in the same turn as the search) must leave
        a visible refusal, not a keep call with no outcome."""
        session, told = _make_session(vector={"q": [_hit("a", "one")]})
        tools = _tools(session)

        out = tools["keep_chunk"](chunk_id="c1", reason="guessed")
        assert "already received" in out
        assert told[-1] == "agent ✗ keep_chunk refused: unknown chunk id 'c1'"
        tools["ignore_chunk"](chunk_id="volcker.md")
        assert told[-1] == (
            "agent ✗ ignore_chunk refused: unknown chunk id 'volcker.md'"
        )
        assert session.kept == []
        assert not session.ignored

    def test_concurrent_searches_never_share_a_chunk_id(self):
        """Strands runs one turn's tool calls in parallel threads, so searches
        can register hits at the same moment; each chunk keeps its own id."""
        workers = 8
        per_search = 500
        start = threading.Barrier(workers)

        def search(query: str) -> list[Any]:
            start.wait()
            return [_hit(f"{query}-{n}", query) for n in range(per_search)]

        session = AgentSearchSession(
            vector_search=search,
            keyword_search=lambda q: [],
            keyword_index_exists=lambda: False,
            extract_keywords=None,
            max_chunks=1,
            tell=lambda _m: None,
        )
        tools = _tools(session)
        threads = [
            threading.Thread(target=tools["vector_search"], kwargs={"query": f"q{i}"})
            for i in range(workers)
        ]
        # Switch threads as often as possible so an unguarded id assignment
        # would actually interleave.
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            sys.setswitchinterval(interval)

        ids = list(session._chunks)
        assert len(ids) == workers * per_search
        assert sorted(ids, key=lambda cid: int(cid[1:])) == [
            f"c{n}" for n in range(1, workers * per_search + 1)
        ]

    def test_keeping_an_ignored_chunk_reverses_the_decision(self):
        session, _ = _make_session(vector={"q": [_hit("a", "one")]}, max_chunks=5)
        tools = _tools(session)
        tools["vector_search"](query="q")
        tools["ignore_chunk"](chunk_id="c1")
        tools["keep_chunk"](chunk_id="c1")
        assert session.kept == ["c1"]
        assert not session.ignored

    def test_search_marks_already_decided_chunks(self):
        session, _ = _make_session(
            vector={"q": [_hit("a", "one")], "r": [_hit("a", "one")]}, max_chunks=5
        )
        tools = _tools(session)
        tools["vector_search"](query="q")
        tools["keep_chunk"](chunk_id="c1")
        assert "[c1] (already kept)" in tools["vector_search"](query="r")

    def test_keyword_search_without_an_index_says_so(self):
        session, _ = _make_session(index_exists=False)
        out = _tools(session)["keyword_search"](query="x")
        assert "no full-text index" in out
        assert "vector_search" in out

    def test_extract_keywords_returns_the_llm_query(self):
        asked: list[str] = []
        session, told = _make_session(
            extract_keywords=lambda q: (asked.append(q), '"alpha" OR "beta"')[1],
        )
        out = _tools(session)["extract_keywords"](question="What about alpha?")
        assert asked == ["What about alpha?"]
        assert '"alpha" OR "beta"' in out
        assert "keyword_search" in out
        assert any('extract_keywords: "alpha" OR "beta"' in line for line in told)

    def test_extract_keywords_without_a_model(self):
        session, _ = _make_session(extract_keywords=None)
        assert "unavailable" in _tools(session)["extract_keywords"](question="q")

    def test_extract_keywords_failure_is_reported(self):
        def boom(_question: str) -> str:
            raise RuntimeError("model down")

        session, _ = _make_session(extract_keywords=boom)
        out = _tools(session)["extract_keywords"](question="q")
        assert "model down" in out
        assert "yourself" in out

    @pytest.mark.parametrize("value", ["", "   "])
    def test_extract_keywords_handles_empty_input_and_output(self, value):
        session, _ = _make_session(extract_keywords=lambda q: value)
        tools = _tools(session)
        assert "empty" in tools["extract_keywords"](question="  ")
        assert "No keywords" in tools["extract_keywords"](question="q")

    def test_long_chunk_text_is_clipped_for_the_agent(self):
        session, _ = _make_session(vector={"q": [_hit("a", "x" * 2000)]})
        out = _tools(session)["vector_search"](query="q")
        assert "x" * agent_tools.CHUNK_PREVIEW_CHARS + "…" in out
        assert "x" * (agent_tools.CHUNK_PREVIEW_CHARS + 1) not in out

    def test_default_sized_chunk_is_shown_whole(self):
        """The agent decides on what it sees, so a chunk indexed with the
        default settings (a shingle of several pieces) must not be clipped."""
        from talkpipe_vault.apps.query import DEFAULT_CHUNK_SIZE, DEFAULT_SHINGLE_SIZE

        pieces = ["y" * DEFAULT_CHUNK_SIZE] * DEFAULT_SHINGLE_SIZE
        text = " ".join(pieces)
        session, _ = _make_session(vector={"q": [_hit("a", text)]})
        out = _tools(session)["vector_search"](query="q")
        assert text in out
        assert "…" not in out


class TestReadDocument:
    """read_document loads the document behind a chunk the agent has seen,
    as a new chunk that is kept or ignored like a search hit."""

    def _session(self, text: str = "The whole document.", **kwargs: Any):
        hits = {"q": [_hit("row1", "a chunk", title="guide.md", source="/d/guide.md")]}
        read: list[str] = []

        def reader(source: str) -> str:
            read.append(source)
            return text

        session, told = _make_session(vector=hits, read_document=reader, **kwargs)
        tools = _tools(session)
        tools["vector_search"](query="q")
        return session, told, tools, read

    def test_the_document_becomes_a_keepable_chunk(self):
        session, told, tools, read = self._session()
        out = tools["read_document"](chunk_id="c1")
        assert read == ["/d/guide.md"]
        assert out.startswith("[c2] guide.md")
        assert "The whole document." in out
        assert any("read_document c1 (guide.md)" in line for line in told)

        tools["keep_chunk"](chunk_id="c2", reason="the full procedure")
        [kept] = session.results
        assert kept.document["content"] == "The whole document."
        assert kept.document["title"] == "guide.md"
        # The source, not the chunk's row id, so the document can be opened
        # from the citation.
        assert kept.doc_id == "/d/guide.md"

    def test_a_long_document_is_read_in_parts(self):
        size = agent_tools.DOCUMENT_PART_CHARS
        text = "a" * size + "b" * 10
        _, _, tools, _ = self._session(text)
        first = tools["read_document"](chunk_id="c1")
        assert f"start={size}" in first
        assert "b" not in first.split("\n\n", 1)[1]
        second = tools["read_document"](chunk_id="c1", start=size)
        assert "b" * 10 in second
        assert "read on" not in second
        assert "nothing from character" in tools["read_document"](
            chunk_id="c1", start=size + 10
        )

    def test_rereading_a_part_returns_the_same_chunk(self):
        _, _, tools, _ = self._session()
        assert tools["read_document"](chunk_id="c1").startswith("[c2]")
        assert tools["read_document"](chunk_id="c1").startswith("[c2]")

    def test_an_unknown_chunk_id_is_refused_and_traced(self):
        _, told, tools, read = self._session()
        assert "Unknown chunk id" in tools["read_document"](chunk_id="c9")
        assert read == []
        assert "agent ✗ read_document refused: unknown chunk id 'c9'" in told

    def test_without_a_reader_the_tool_says_so(self):
        session, _ = _make_session(vector={"q": [_hit("a", "x", source="/d/a.md")]})
        tools = _tools(session)
        tools["vector_search"](query="q")
        assert "unavailable" in tools["read_document"](chunk_id="c1")

    def test_a_chunk_without_a_source_cannot_be_read(self):
        session, _ = _make_session(
            vector={"q": [_hit("a", "x")]}, read_document=lambda s: "text"
        )
        tools = _tools(session)
        tools["vector_search"](query="q")
        assert "no source document" in tools["read_document"](chunk_id="c1")

    def test_a_failed_read_is_reported_to_the_agent(self):
        def fail(source: str) -> str:
            raise FileNotFoundError("guide.md is gone")

        session, _ = _make_session(
            vector={"q": [_hit("a", "x", source="/d/guide.md")]}, read_document=fail
        )
        tools = _tools(session)
        tools["vector_search"](query="q")
        assert "guide.md is gone" in tools["read_document"](chunk_id="c1")
        assert session.trace[-1].endswith("failed: guide.md is gone")


class _ScriptedSearch(AgentVaultSearch):
    """AgentVaultSearch with the vault, embedder, and model replaced: the
    agent loop is a script that calls the tools directly.

    It runs the real constructor; only ``_bind_vault`` (the one step that
    touches disk and models) and the agent loop are replaced, so a new
    attribute on the segment cannot silently go missing here."""

    def __init__(self, script, **kwargs):  # type: ignore[no-untyped-def]
        self.told: list[str] = []
        self._script = script
        self._stop_reason = kwargs.pop("stop_reason", "end_turn")
        kwargs.setdefault("max_chunks", 2)
        kwargs.setdefault("max_iterations", 3)
        super().__init__(
            vault_path="unused",
            chat_model="a-model",
            chat_source="a-source",
            rename_fields=None,
            progress=self.told.append,
            **kwargs,
        )

    def _bind_vault(self) -> None:
        self._extract_keywords = lambda question: f'"{question}"'

    def vector_search(self, query: str) -> list[Any]:
        return [_hit("a", "one"), _hit("b", "two"), _hit("c", "three")]

    def keyword_index_exists(self) -> bool:
        return False

    def keyword_search(self, query: str) -> list[Any]:
        return []

    def run_agent(self, session: AgentSearchSession, query: str) -> str:
        self._script(_tools(session))
        return self._stop_reason


class TestAgentIsAnLLMAgent:
    """The agent is talkpipe-agents' llmAgent segment configured by this
    module, not a Strands agent assembled here."""

    def test_make_agent_configures_llmagent_from_the_segment(self):
        from talkpipe_agents import LLMAgent

        segment = _ScriptedSearch(lambda _tools: None, max_iterations=5)
        agent = segment.make_agent(segment.make_session())

        assert isinstance(agent, LLMAgent)
        assert agent.model == "a-model"
        assert agent.source == "a-source"
        assert agent.system_prompt == agent_search.RETRIEVAL_SYSTEM_PROMPT
        # The iteration cap is the library's turn limit, not a second loop.
        assert agent.max_turns == 5
        assert {t.tool_name for t in agent.tools} == set(
            agent_tools.vault_agent_tool_names()
        )

    def test_with_max_iterations_raises_the_cap_on_a_copy(self):
        """Extra Effort: same bindings, more turns, original untouched."""
        segment = _ScriptedSearch(lambda _tools: None, max_iterations=5)
        extra = segment.with_max_iterations(10)

        assert extra is not segment
        assert segment.max_iterations == 5
        assert extra.make_agent(extra.make_session()).max_turns == 10
        assert extra._extract_keywords is segment._extract_keywords
        assert segment.with_max_iterations(0).max_iterations == 1

    def test_the_tools_parameter_selects_what_the_agent_gets(self):
        segment = _ScriptedSearch(lambda _tools: None, tools="vector_search,think")
        agent = segment.make_agent(segment.make_session())
        assert {t.tool_name for t in agent.tools} == {"vector_search", "think"}

    def test_the_system_prompt_is_replaceable(self):
        segment = _ScriptedSearch(lambda _tools: None)
        segment.system_prompt = "Be brief."
        assert segment.make_agent(segment.make_session()).system_prompt == "Be brief."

    def test_progress_reaches_the_session_trace(self):
        """llmAgent's own progress lines land in the trace the pages show."""
        segment = _ScriptedSearch(lambda _tools: None)
        session = segment.make_session()
        segment.make_agent(session).tell("agent → vector_search({'query': 'q'})")
        assert session.trace == ["agent → vector_search({'query': 'q'})"]
        assert segment.told == ["agent → vector_search({'query': 'q'})"]


class TestAgentVaultSearch:
    def test_outcome_reports_the_agents_own_judgement(self):
        """The intended ending: the agent said it could answer, after one chunk."""

        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="what is one?")
            tools["keep_chunk"](chunk_id="c1")
            tools["ignore_chunk"](chunk_id="c2")
            tools["enough_information"](reason="c1 answers it")

        segment = _ScriptedSearch(script, max_chunks=5)
        outcome = segment.process_value("what is one?")
        assert isinstance(outcome, AgentSearchOutcome)
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert outcome.note.startswith("Agent judged it had enough to answer")
        # The reason it gave is carried into the note the pages display.
        assert "(c1 answers it)" in outcome.note
        assert "kept 1 chunk after 1 search" in outcome.note
        assert outcome.trace[0].startswith("agent search: 'what is one?'")
        assert outcome.trace == segment.told

    def test_outcome_reports_the_chunk_ceiling(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="what is one?")
            tools["keep_chunk"](chunk_id="c1")
            tools["ignore_chunk"](chunk_id="c2")
            tools["keep_chunk"](chunk_id="c3")

        segment = _ScriptedSearch(script, max_chunks=2)
        outcome = segment.process_value("what is one?")
        assert [r.doc_id for r in outcome.results] == ["a", "c"]
        assert outcome.note.startswith("Agent filled its 2-chunk maximum")
        assert "kept 2 chunks after 1 search" in outcome.note

    def test_outcome_reports_iteration_cap(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c1")

        segment = _ScriptedSearch(script, max_chunks=5, stop_reason="limit_turns")
        outcome = segment.process_value("q")
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert "hit the 3-iteration cap" in outcome.note

    def test_outcome_distinguishes_stopping_without_deciding(self):
        """A model that stops without judging is not the same as one that did."""

        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c1")

        outcome = _ScriptedSearch(script, max_chunks=5).process_value("q")
        assert outcome.note.startswith("Agent stopped without saying it had enough")
        # ...and says that the model is the likely cause.
        assert agent_search.WEAK_TOOL_CALLING_HINT in outcome.note

    def test_agent_failure_keeps_what_was_selected(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c2")
            raise RuntimeError("model unreachable")

        outcome = _ScriptedSearch(script).process_value("q")
        assert [r.doc_id for r in outcome.results] == ["b"]
        assert "model unreachable" in outcome.note
        assert outcome.note.endswith("showing what it kept.")
        assert outcome.error == "model unreachable"

    def test_agent_failure_before_keeping_anything_says_so(self):
        def script(tools: dict[str, Any]) -> None:
            raise RuntimeError("tools param requires --jinja flag")

        outcome = _ScriptedSearch(script).process_value("q")
        assert outcome.results == []
        assert outcome.note.startswith("The agent stopped with an error (")
        assert outcome.note.endswith("it kept nothing.")
        assert outcome.error == "tools param requires --jinja flag"

    def test_a_run_that_did_not_fail_has_no_error(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")

        assert _ScriptedSearch(script).process_value("q").error == ""

    def test_a_missing_vault_is_refused_when_the_segment_is_built(self, tmp_path):
        """A mistyped vault path must fail up front -- not create an empty
        folder and send the agent through a run of empty searches."""
        missing = tmp_path / "no-such-vault"
        with pytest.raises(ValueError, match=r"No vault at .*no-such-vault"):
            AgentVaultSearch(
                vault_path=str(missing), chat_model="m", chat_source="ollama"
            )
        assert not missing.exists()

    def test_read_document_uses_the_indexing_extractors(self, tmp_path):
        doc = tmp_path / "notes.md"
        doc.write_text("# Notes\n\nAll of it.\n")
        segment = _ScriptedSearch(lambda _tools: None)
        assert "All of it." in segment.read_document(str(doc))
        # A document that moved since indexing is reported, not created.
        with pytest.raises(FileNotFoundError, match=r"gone\.md"):
            segment.read_document(str(tmp_path / "gone.md"))

    def test_as_function_yields_the_outcome(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c1")

        fn = _ScriptedSearch(script, max_chunks=1).as_function(
            single_in=True, single_out=True
        )
        assert [r.doc_id for r in fn("q").results] == ["a"]


class _ScriptedChat(_ScriptedSearch, agent_search.AgentVaultChat):
    """The scripted agent loop with the RAG answering stage stubbed out, so
    the Ask variant is exercised without a vault, an embedder, or an LLM."""

    def __init__(self, script, **kwargs):  # type: ignore[no-untyped-def]
        _ScriptedSearch.__init__(self, script, **kwargs)
        self.answered: list[tuple[str, list[Any]]] = []

    def answer(self, question: str, background: list[Any]) -> str:
        self.answered.append((question, background))
        return f"Answer to {question} from {len(background)} chunk(s)."


class TestAgentVaultChat:
    def test_answer_is_built_from_the_chunks_the_agent_kept(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="what is one?")
            tools["keep_chunk"](chunk_id="c1")
            tools["ignore_chunk"](chunk_id="c2")
            tools["keep_chunk"](chunk_id="c3")
            tools["enough_information"](reason="both sides covered")

        segment = _ScriptedChat(script, max_chunks=5)
        outcome = segment.process_value("what is one?")

        assert isinstance(outcome, agent_search.AgentChatOutcome)
        assert outcome.response == "Answer to what is one? from 2 chunk(s)."
        # The citations are exactly the kept chunks, in keep order.
        assert [r.doc_id for r in outcome.results] == ["a", "c"]
        assert outcome.note.startswith("Agent judged it had enough to answer")
        assert outcome.trace == segment.told
        # The RAG prompt saw those same chunks, not a separate retrieval.
        question, background = segment.answered[0]
        assert question == "what is one?"
        assert [r.doc_id for r in background] == ["a", "c"]

    def test_nothing_kept_means_no_prompt_is_sent(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["ignore_chunk"](chunk_id="c1")

        segment = _ScriptedChat(script)
        outcome = segment.process_value("q")

        assert segment.answered == []
        assert outcome.results == []
        assert outcome.response == agent_search.NO_CONTEXT_ANSWER
        assert outcome.error == ""

    def test_a_failed_run_is_not_answered_as_an_empty_vault(self):
        """An agent that errored before searching never looked at the vault;
        the answer must not blame the vault or the question."""

        def script(tools: dict[str, Any]) -> None:
            raise RuntimeError("model 'x' not found")

        segment = _ScriptedChat(script)
        outcome = segment.process_value("q")

        assert segment.answered == []
        assert outcome.response == agent_search.AGENT_ERROR_ANSWER
        assert outcome.error == "model 'x' not found"
        assert "model 'x' not found" in outcome.note

    def test_as_function_yields_the_outcome(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c1")

        fn = _ScriptedChat(script, max_chunks=1).as_function(
            single_in=True, single_out=True
        )
        outcome = fn("q")
        assert outcome.response.startswith("Answer to q")
        assert [r.doc_id for r in outcome.results] == ["a"]

    def test_select_chunks_answers_nothing(self):
        """The Search page drives the same segment through select_chunks."""

        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c1")

        segment = _ScriptedChat(script, max_chunks=1)
        outcome = segment.select_chunks("q")

        assert isinstance(outcome, AgentSearchOutcome)
        assert not isinstance(outcome, agent_search.AgentChatOutcome)
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert segment.answered == []


class TestToolsRefuseOnceTheRunIsOver:
    """The stopping rule is enforced by the tools themselves, so it holds
    behind any agent rather than depending on a hook wired into one."""

    def test_tools_refuse_after_the_agent_says_it_has_enough(self):
        session, told = _make_session(vector={"q": [_hit("a", "one")]}, max_chunks=5)
        tools = _tools(session)

        tools["vector_search"](query="q")
        tools["keep_chunk"](chunk_id="c1")
        tools["enough_information"](reason="that covers it")

        out = tools["vector_search"](query="more")
        assert "you already said you had enough" in out
        assert "done" in out
        assert "refused: you already said you had enough" in told[-1]
        # ...and the refused call really did nothing.
        assert session.queries_run == [("vector", "q")]

    def test_tools_refuse_once_the_chunk_ceiling_is_full(self):
        session, told = _make_session(vector={"q": [_hit("a", "one")]}, max_chunks=1)
        tools = _tools(session)

        tools["vector_search"](query="q")
        tools["keep_chunk"](chunk_id="c1")

        out = tools["vector_search"](query="more")
        assert "1-chunk maximum is reached" in out
        assert "refused: the 1-chunk maximum is reached" in told[-1]
        assert session.queries_run == [("vector", "q")]


class TestToolSelection:
    """Tools are picked by name, so a pipeline can add or drop one without
    touching the tool pack or the segment."""

    def test_the_default_pack_is_the_seven_vault_tools(self):
        session, _ = _make_session()
        assert set(_tools(session)) == {
            "vector_search",
            "keyword_search",
            "extract_keywords",
            "read_document",
            "keep_chunk",
            "ignore_chunk",
            "enough_information",
        }
        assert (
            agent_tools.parse_tool_names(None) == agent_tools.vault_agent_tool_names()
        )

    @pytest.mark.parametrize(
        "spec", ["vector_search,keep_chunk", ["vector_search", "keep_chunk"]]
    )
    def test_a_named_subset_binds_only_those_tools(self, spec):
        session, _ = _make_session()
        assert set(_tools(session, spec)) == {"vector_search", "keep_chunk"}

    def test_a_generic_talkpipe_agents_tool_can_be_mixed_in(self):
        """Names the vault does not own fall through to the shared registry."""
        session, _ = _make_session()
        tools = _tools(session, "vector_search,think")
        assert set(tools) == {"vector_search", "think"}

    def test_an_unknown_tool_name_is_refused_with_both_registries_listed(self):
        session, _ = _make_session()
        with pytest.raises(ValueError) as excinfo:
            build_tools(session, ["vector_search", "no_such_tool"])
        message = str(excinfo.value)
        assert "no_such_tool" in message
        assert "vault retrieval tools" in message
        assert "enough_information" in message

    def test_a_blank_spec_falls_back_to_the_full_pack(self):
        assert agent_tools.parse_tool_names("") == agent_tools.vault_agent_tool_names()
        assert agent_tools.parse_tool_names([]) == agent_tools.vault_agent_tool_names()


@pytest.mark.parametrize("bad", [0, -3])
def test_the_chunk_ceiling_has_a_floor(bad):
    session, _ = _make_session(max_chunks=bad)
    assert session.max_chunks == 1
