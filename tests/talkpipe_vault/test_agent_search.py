"""Tests for agent-driven result selection (pipelines/agent_search.py).

The session and its tools are exercised with injected search callables and a
scripted stand-in for the agent loop, so nothing here needs a vault, an
embedder, or an LLM.
"""

from __future__ import annotations

from typing import Any

import pytest
from talkpipe.search.abstract import SearchResult

from talkpipe_vault.pipelines import agent_search
from talkpipe_vault.pipelines.agent_search import (
    AgentSearchOutcome,
    AgentSearchSession,
    AgentVaultSearch,
)


def _hit(doc_id: str, content: str, **doc: Any) -> SearchResult:
    return SearchResult(score=0.5, doc_id=doc_id, document={"content": content, **doc})


def _tools(session: AgentSearchSession) -> dict[str, Any]:
    return {t.tool_name: t for t in session.tools()}


def _make_session(
    *,
    vector: dict[str, list[Any]] | None = None,
    keyword: dict[str, list[Any]] | None = None,
    index_exists: bool = True,
    create_index: Any = None,
    target: int = 2,
) -> tuple[AgentSearchSession, list[str]]:
    told: list[str] = []
    vector = vector or {}
    keyword = keyword or {}
    session = AgentSearchSession(
        vector_search=lambda q: vector.get(q, []),
        keyword_search=lambda q: keyword.get(q, []),
        keyword_index_exists=lambda: index_exists,
        create_keyword_index=create_index,
        target_count=target,
        tell=told.append,
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
            create_keyword_index=None,
            target_count=1,
            tell=lambda _m: None,
        )
        tools = _tools(session)
        assert "No vector results" in tools["vector_search"](query="nothing")
        assert "index unreadable" in tools["keyword_search"](query="x")

    def test_keep_and_ignore_drive_results_and_target(self):
        session, told = _make_session(
            vector={"q": [_hit("a", "one"), _hit("b", "two"), _hit("c", "three")]},
            target=2,
        )
        tools = _tools(session)
        tools["vector_search"](query="q")

        assert "Unknown chunk id" in tools["keep_chunk"](chunk_id="c9")
        assert "Kept 1 of 2" in tools["keep_chunk"](chunk_id="c2", reason="on topic")
        assert "already kept" in tools["keep_chunk"](chunk_id="c2")
        tools["ignore_chunk"](chunk_id="c1", reason="off topic")
        assert not session.target_reached

        out = tools["keep_chunk"](chunk_id="c3")
        assert "Target reached" in out
        assert "done" in out
        assert session.target_reached
        assert [r.doc_id for r in session.results] == ["b", "c"]
        assert session.ignored == {"c1"}
        assert any("keep c2 — on topic" in line for line in told)
        assert any("ignore c1 — off topic" in line for line in told)

    def test_keeping_an_ignored_chunk_reverses_the_decision(self):
        session, _ = _make_session(vector={"q": [_hit("a", "one")]}, target=5)
        tools = _tools(session)
        tools["vector_search"](query="q")
        tools["ignore_chunk"](chunk_id="c1")
        tools["keep_chunk"](chunk_id="c1")
        assert session.kept == ["c1"]
        assert not session.ignored

    def test_search_marks_already_decided_chunks(self):
        session, _ = _make_session(
            vector={"q": [_hit("a", "one")], "r": [_hit("a", "one")]}, target=5
        )
        tools = _tools(session)
        tools["vector_search"](query="q")
        tools["keep_chunk"](chunk_id="c1")
        assert "[c1] (already kept)" in tools["vector_search"](query="r")

    def test_keyword_search_without_index_points_at_create_tool(self):
        built: list[bool] = []
        state = {"exists": False}
        session = AgentSearchSession(
            vector_search=lambda q: [],
            keyword_search=lambda q: [_hit("a", "found")],
            keyword_index_exists=lambda: state["exists"],
            create_keyword_index=lambda: (
                built.append(True),
                state.update(exists=True),
            ),
            target_count=1,
            tell=lambda _m: None,
        )
        tools = _tools(session)
        assert "create_keyword_index" in tools["keyword_search"](query="x")
        assert "built" in tools["create_keyword_index"]()
        assert built == [True]
        assert "already exists" in tools["create_keyword_index"]()
        assert "[c1]\nfound" in tools["keyword_search"](query="x")

    def test_keyword_search_without_index_or_builder(self):
        session, _ = _make_session(index_exists=False, create_index=None)
        tools = _tools(session)
        assert "cannot be created" in tools["keyword_search"](query="x")
        assert "cannot be created" in tools["create_keyword_index"]()

    def test_create_index_failure_is_reported(self):
        def fail() -> None:
            raise OSError("disk full")

        session, _ = _make_session(index_exists=False, create_index=fail)
        assert "disk full" in _tools(session)["create_keyword_index"]()

    def test_long_chunk_text_is_clipped_for_the_agent(self):
        session, _ = _make_session(vector={"q": [_hit("a", "x" * 2000)]})
        out = _tools(session)["vector_search"](query="q")
        assert "x" * agent_search.CHUNK_PREVIEW_CHARS + "…" in out
        assert "x" * (agent_search.CHUNK_PREVIEW_CHARS + 1) not in out


class _ScriptedSearch(AgentVaultSearch):
    """AgentVaultSearch with the vault, embedder, and model replaced: the
    agent loop is a script that calls the tools directly."""

    def __init__(self, script, **kwargs):  # type: ignore[no-untyped-def]
        # Bypass the real constructor (it opens the vault and builds a model).
        from talkpipe.pipe.basic import AbstractFieldSegment

        AbstractFieldSegment.__init__(self, field=None, set_as=None, multi_emit=False)
        self.vault_path = "unused"
        self.target_count = kwargs.get("target_count", 2)
        self.max_iterations = kwargs.get("max_iterations", 3)
        self.search_limit = 8
        self.rename_fields = {}
        self.create_keyword_index = None
        self.told: list[str] = []
        self.tell = self.told.append
        self._script = script
        self._stop_reason = kwargs.get("stop_reason", "end_turn")

    def vector_search(self, query: str) -> list[Any]:
        return [_hit("a", "one"), _hit("b", "two"), _hit("c", "three")]

    def keyword_index_exists(self) -> bool:
        return False

    def keyword_search(self, query: str) -> list[Any]:
        return []

    def run_agent(self, session: AgentSearchSession, query: str) -> str:
        self._script(_tools(session))
        return self._stop_reason


class TestAgentVaultSearch:
    def test_outcome_reports_target_reached(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="what is one?")
            tools["keep_chunk"](chunk_id="c1")
            tools["ignore_chunk"](chunk_id="c2")
            tools["keep_chunk"](chunk_id="c3")

        segment = _ScriptedSearch(script, target_count=2)
        outcome = segment.process_value("what is one?")
        assert isinstance(outcome, AgentSearchOutcome)
        assert [r.doc_id for r in outcome.results] == ["a", "c"]
        assert outcome.note.startswith("Agent reached its target")
        assert "kept 2 of 2" in outcome.note
        assert "1 search" in outcome.note
        assert outcome.trace[0].startswith("agent search: 'what is one?'")
        assert outcome.trace == segment.told

    def test_outcome_reports_iteration_cap(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c1")

        segment = _ScriptedSearch(script, target_count=5, stop_reason="limit_turns")
        outcome = segment.process_value("q")
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert "hit the 3-iteration cap" in outcome.note

    def test_agent_failure_keeps_what_was_selected(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c2")
            raise RuntimeError("model unreachable")

        outcome = _ScriptedSearch(script).process_value("q")
        assert [r.doc_id for r in outcome.results] == ["b"]
        assert "model unreachable" in outcome.note

    def test_as_function_yields_the_outcome(self):
        def script(tools: dict[str, Any]) -> None:
            tools["vector_search"](query="q")
            tools["keep_chunk"](chunk_id="c1")

        fn = _ScriptedSearch(script, target_count=1).as_function(
            single_in=True, single_out=True
        )
        assert [r.doc_id for r in fn("q").results] == ["a"]


class TestSessionHooks:
    def test_calls_after_target_are_cancelled(self):
        session, told = _make_session(vector={"q": [_hit("a", "one")]}, target=1)
        hooks = agent_search._SessionHooks(session)

        class Event:
            def __init__(self, name: str, **args: Any) -> None:
                self.tool_use = {"name": name, "input": args}
                self.cancel_tool: str | bool = False

        event = Event("vector_search", query="q")
        hooks._before_tool(event)  # type: ignore[arg-type]
        assert event.cancel_tool is False
        assert told[-1] == "agent → vector_search(query='q')"

        _tools(session)["vector_search"](query="q")
        _tools(session)["keep_chunk"](chunk_id="c1")
        event = Event("vector_search", query="more")
        hooks._before_tool(event)  # type: ignore[arg-type]
        assert isinstance(event.cancel_tool, str)
        assert "done" in event.cancel_tool
        assert "cancelled: target reached" in told[-1]


@pytest.mark.parametrize("bad", [0, -3])
def test_target_and_iterations_have_a_floor(bad):
    session, _ = _make_session(target=bad)
    assert session.target_count == 1
