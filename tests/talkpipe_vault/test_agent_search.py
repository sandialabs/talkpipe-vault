"""Tests for agent-driven retrieval: the vault's agent tool pack
(pipelines/agent_tools.py) and the segments built on it
(pipelines/agent_search.py).

The agent is the real llmAgent running talkpipe-agents' findings loop, with
the model replaced by talkpipe-agents' ScriptedModel (the ``scripted_model``
fixture: each test scripts the agent's tool calls) and the judge by a
ScriptedJudge deciding by doc_id. The vault's searches are scripted too, so
nothing here needs a vault, an embedder, or an LLM. A segment built with
``judge=False`` runs the session without a judge, where the agent keeps and
ignores by hand.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import pytest
from talkpipe.search.abstract import SearchResult
from talkpipe_agents import FindingsJudge, FindingsSession, JudgeFindings, Judgement
from talkpipe_agents.testing import (
    ScriptedJudge,
    ScriptedModel,
    ScriptedToolErrorWarning,
)

from talkpipe_vault.pipelines import agent_search, agent_tools
from talkpipe_vault.pipelines.agent_search import AgentSearchOutcome, AgentVaultSearch
from talkpipe_vault.pipelines.agent_tools import SearchBackend, build_tools
from talkpipe_vault.pipelines.judge_results import search_findings


def _hit(doc_id: str, content: str, **doc: Any) -> SearchResult:
    return SearchResult(score=0.5, doc_id=doc_id, document={"content": content, **doc})


THREE = [
    _hit("a", "one", title="A.md"),
    _hit("b", "two", title="B.md"),
    _hit("c", "three", title="C.md"),
]

Verdicts = dict[str, bool | tuple[bool, str]]


def _rule(verdicts: Verdicts) -> Any:
    """A ScriptedJudge rule deciding by a finding's key (a hit's doc_id, a
    document part's source): kept "on topic" when unlisted."""

    def decide(finding: Any) -> tuple[bool, str]:
        doc_id = finding.to_search_result().doc_id
        verdict = verdicts.get(doc_id, (True, "on topic"))
        return (verdict, "") if isinstance(verdict, bool) else verdict

    return decide


class _Scripted(AgentVaultSearch):
    """AgentVaultSearch with the vault, embedder, judge and model replaced:
    the agent's model is the ``scripted`` source (the scripted_model
    fixture), the searches return scripted hits, and the judge decides by
    ``verdicts`` (doc_id → keep, or (keep, reason); unlisted hits are kept
    "on topic"). ``judge=False`` runs without a judge.

    It runs the real constructor; only ``_bind_vault`` (the one step that
    touches disk and models) and the search bindings are replaced, so a new
    attribute on the segment cannot silently go missing here."""

    def __init__(  # type: ignore[no-untyped-def]
        self,
        verdicts=None,
        *,
        vector=None,
        keyword=None,
        index_exists=False,
        read=None,
        judge=True,
        **kwargs,
    ):
        self.told: list[str] = []
        self.verdicts: Verdicts = verdicts or {}
        # query → hits; None returns THREE for any query.
        self.vector: dict[str, list[Any]] | None = vector
        self.keyword: dict[str, list[Any]] = keyword or {}
        self.index_exists = index_exists
        self.read = read
        self.judge = ScriptedJudge(keep=_rule(self.verdicts)) if judge else None
        kwargs.setdefault("max_chunks", 2)
        kwargs.setdefault("max_iterations", 8)
        super().__init__(
            vault_path="unused",
            chat_model="any",
            chat_source="scripted",
            progress=self.told.append,
            **kwargs,
        )

    def _bind_vault(self) -> None:
        self._extract_keywords = lambda question: f'"{question}"'

    @property
    def findings_judge(self) -> FindingsJudge | None:
        return self.judge

    def vector_search(self, query: str) -> list[Any]:
        return THREE if self.vector is None else self.vector.get(query, [])

    def keyword_index_exists(self) -> bool:
        return self.index_exists

    def keyword_search(self, query: str) -> list[Any]:
        return self.keyword.get(query, [])

    def read_document(self, source: str) -> str:
        if self.read is None:
            return super().read_document(source)
        return self.read(source)


def _run(
    model: ScriptedModel, segment: AgentVaultSearch, *calls: tuple, question: str = "q"
) -> AgentSearchOutcome:
    """Script *calls* (``(tool, {args})``) and a closing "done", then run
    *segment* on *question*."""
    for name, args in calls:
        model.call(name, **args)
    model.say("done")
    return segment.process_value(question)


def _replies(model: ScriptedModel) -> list[str]:
    return [result.text for result in model.tool_results]


def _tool_errors() -> Any:
    return pytest.warns(ScriptedToolErrorWarning)


class TestSessionTools:
    """The unjudged path: no judge, so hits are shown undecided and the
    agent keeps or ignores them by hand (keep_chunk / ignore_chunk still
    name keep_finding / ignore_finding)."""

    TOOLS = "vector_search,keyword_search,keep_chunk,ignore_chunk,enough_information"

    def _segment(self, **kwargs: Any) -> _Scripted:
        kwargs.setdefault("tools", self.TOOLS)
        return _Scripted(judge=False, **kwargs)

    def test_search_assigns_ids_and_dedupes_across_searches(self, scripted_model):
        segment = self._segment(
            vector={"q": [_hit("a", "alpha text", title="A"), _hit("b", "beta")]},
            keyword={"alpha": [_hit("a", "alpha text"), _hit("c", "gamma")]},
            index_exists=True,
        )
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("keyword_search", {"query": "alpha"}),
            ("keep_finding", {"finding_id": "f1", "reason": "on topic"}),
        )
        first, second, _ = _replies(scripted_model)
        assert "Decide on each undecided one" in first
        assert "[f1] A\nalpha text" in first
        assert "[f2]\nbeta" in first
        # the same doc_id keeps its id; the new one gets the next id
        assert "[f1]" in second
        assert "[f3]\ngamma" in second
        assert "f4" not in second
        # The trace a person reads names the document behind the short id.
        assert any("keep f1 (A) — on topic" in line for line in segment.told)

    def test_a_repeated_query_is_not_judged_again(self, scripted_model):
        segment = _Scripted(vector={"q": [_hit("a", "x")]}, max_chunks=5)
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("vector_search", {"query": "q"}),
        )
        assert "No new findings from vector_search" in _replies(scripted_model)[1]
        assert len(segment.judge.batches) == 1

    def test_empty_results_and_failures_are_reported_to_the_agent(self, scripted_model):
        segment = self._segment(vector={}, index_exists=True)

        def boom(_query: str) -> list[Any]:
            raise RuntimeError("index unreadable")

        segment.keyword_search = boom  # type: ignore[method-assign]
        with _tool_errors():
            _run(
                scripted_model,
                segment,
                ("vector_search", {"query": "nothing"}),
                ("keyword_search", {"query": "x"}),
            )
        empty, failed = scripted_model.tool_results
        assert "Nothing found from vector_search" in empty.text
        assert failed.status == "error"
        assert "The keyword search failed: index unreadable" in failed.text

    def test_a_search_with_no_callable_at_all_says_so(self):
        tools = {
            t.tool_name: t for t in build_tools(SearchBackend(), ["vector_search"])
        }
        with pytest.raises(ValueError, match="unavailable"):
            tools["vector_search"](query="q")
        tools = {
            t.tool_name: t
            for t in build_tools(
                SearchBackend(keyword_index_exists=lambda: True), ["keyword_search"]
            )
        }
        with pytest.raises(ValueError, match="unavailable"):
            tools["keyword_search"](query="q")

    def test_keep_and_ignore_drive_results_and_the_chunk_ceiling(self, scripted_model):
        segment = self._segment(
            vector={"q": [_hit("a", "one"), _hit("b", "two"), _hit("c", "three")]},
            max_chunks=2,
        )
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("keep_finding", {"finding_id": "f9"}),
            ("keep_finding", {"finding_id": "f2", "reason": "on topic"}),
            ("keep_finding", {"finding_id": "f2"}),
            ("ignore_finding", {"finding_id": "f1", "reason": "off topic"}),
            ("keep_finding", {"finding_id": "f3"}),
        )
        _, unknown, kept, again, _, full = _replies(scripted_model)
        assert "Unknown finding id" in unknown
        assert "Kept 1 finding(s), at most 2." in kept
        # Every keep points at the real stopping criterion, not at a count.
        assert "enough_information" in kept
        assert "already kept" in again
        assert "maximum of 2 findings" in full
        assert "done" in full
        # the ceiling stopped it, not its judgement
        assert outcome.note.startswith("Agent filled its 2-chunk maximum")
        assert [r.doc_id for r in outcome.results] == ["b", "c"]
        assert "ignored 1." in outcome.note
        assert any("keep f2 — on topic" in line for line in segment.told)
        assert any("ignore f1 — off topic" in line for line in segment.told)

    def test_enough_information_ends_the_run_at_any_chunk_count(self, scripted_model):
        """The stopping criterion is the agent's judgement, not a count."""
        segment = self._segment(
            vector={"q": [_hit("a", "one"), _hit("b", "two")]}, max_chunks=5
        )
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("keep_finding", {"finding_id": "f1"}),
            ("enough_information", {"reason": "covers the whole question"}),
        )
        assert "done" in _replies(scripted_model)[-1]
        assert outcome.note.startswith(
            "Agent judged it had enough to answer (covers the whole question)"
        )
        assert [r.doc_id for r in outcome.results] == ["a"]
        # one of five kept and one undecided, but that is not the test
        assert outcome.undecided == 1
        assert any("enough information — covers the whole" in x for x in segment.told)

    def test_enough_information_is_refused_with_nothing_kept(self, scripted_model):
        """Stopping with no context produces no answer; keep searching instead."""
        segment = self._segment(vector={"q": [_hit("a", "one")]}, max_chunks=5)
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("enough_information", {"reason": "nothing here"}),
        )
        assert "Nothing has been kept yet" in _replies(scripted_model)[-1]
        assert not outcome.note.startswith("Agent judged it had enough")
        assert any(
            "enough_information refused: nothing kept yet" in x for x in segment.told
        )

    def test_an_unknown_chunk_id_is_refused_in_the_trace_too(self, scripted_model):
        """A model that keeps a finding before any search has returned it (a
        guessed id, or a keep sent in the same turn as the search) must leave
        a visible refusal, not a keep call with no outcome."""
        segment = self._segment(vector={"q": [_hit("a", "one")]})
        outcome = _run(
            scripted_model,
            segment,
            ("keep_finding", {"finding_id": "f1", "reason": "guessed"}),
            ("ignore_finding", {"finding_id": "volcker.md"}),
        )
        assert "already received" in _replies(scripted_model)[0]
        assert "agent ✗ keep_finding refused: unknown finding id 'f1'" in segment.told
        assert (
            "agent ✗ ignore_finding refused: unknown finding id 'volcker.md'"
            in segment.told
        )
        assert outcome.results == []

    def test_concurrent_searches_never_share_a_chunk_id(self):
        """Strands runs one turn's tool calls in parallel threads, so the hook
        can process two searches' findings at once; each keeps its own id."""
        workers = 8
        per_search = 500
        start = threading.Barrier(workers)
        session = FindingsSession("q", max_kept=1)
        loop = JudgeFindings(session)

        def search(n: int) -> None:
            findings = search_findings(
                [_hit(f"q{n}-{i}", f"q{n}") for i in range(per_search)]
            )
            start.wait()
            loop.process(findings, tool="vector_search")

        threads = [threading.Thread(target=search, args=(i,)) for i in range(workers)]
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

        ids = session.undecided
        assert len(ids) == workers * per_search
        assert sorted(ids, key=lambda fid: int(fid[1:])) == [
            f"f{n}" for n in range(1, workers * per_search + 1)
        ]

    def test_keeping_an_ignored_chunk_reverses_the_decision(self, scripted_model):
        segment = self._segment(vector={"q": [_hit("a", "one")]}, max_chunks=5)
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("ignore_finding", {"finding_id": "f1"}),
            ("keep_finding", {"finding_id": "f1"}),
        )
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert "ignored 0." in outcome.note

    def test_search_marks_already_decided_chunks(self, scripted_model):
        segment = self._segment(
            vector={"q": [_hit("a", "one")], "r": [_hit("a", "one")]}, max_chunks=5
        )
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("keep_finding", {"finding_id": "f1"}),
            ("vector_search", {"query": "r"}),
        )
        assert "[f1] (already kept)" in _replies(scripted_model)[-1]

    def test_keyword_search_without_an_index_says_so(self, scripted_model):
        segment = self._segment(index_exists=False)
        with _tool_errors():
            _run(scripted_model, segment, ("keyword_search", {"query": "x"}))
        [reply] = scripted_model.tool_results
        assert reply.status == "error"
        assert "no full-text index" in reply.text
        assert "vector_search" in reply.text

    def test_extract_keywords_returns_the_llm_query(self, scripted_model):
        asked: list[str] = []
        segment = _Scripted()
        segment._extract_keywords = lambda q: (asked.append(q), '"alpha" OR "beta"')[1]
        _run(
            scripted_model,
            segment,
            ("extract_keywords", {"question": "What about alpha?"}),
        )
        [out] = _replies(scripted_model)
        assert asked == ["What about alpha?"]
        assert '"alpha" OR "beta"' in out
        assert "keyword_search" in out
        assert any('extract_keywords: "alpha" OR "beta"' in x for x in segment.told)

    def test_extract_keywords_without_a_model(self):
        [tool] = build_tools(SearchBackend(), ["extract_keywords"])
        assert "unavailable" in tool(question="q")

    def test_extract_keywords_failure_is_reported(self):
        def boom(_question: str) -> str:
            raise RuntimeError("model down")

        [tool] = build_tools(SearchBackend(extract_keywords=boom), ["extract_keywords"])
        out = tool(question="q")
        assert "model down" in out
        assert "yourself" in out

    @pytest.mark.parametrize("value", ["", "   "])
    def test_extract_keywords_handles_empty_input_and_output(self, value):
        [tool] = build_tools(
            SearchBackend(extract_keywords=lambda q: value), ["extract_keywords"]
        )
        assert "empty" in tool(question="  ")
        assert "No keywords" in tool(question="q")

    def test_long_chunk_text_is_clipped_for_the_agent(self, scripted_model):
        segment = _Scripted(vector={"q": [_hit("a", "x" * 2000)]})
        _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        [out] = _replies(scripted_model)
        assert "x" * agent_tools.CHUNK_PREVIEW_CHARS + "…" in out
        assert "x" * (agent_tools.CHUNK_PREVIEW_CHARS + 1) not in out

    def test_default_sized_chunk_is_shown_whole(self, scripted_model):
        """The agent decides on what it sees, so a chunk indexed with the
        default settings (a shingle of several pieces) must not be clipped."""
        from talkpipe_vault.pipelines.config import (
            DEFAULT_CHUNK_SIZE,
            DEFAULT_SHINGLE_SIZE,
        )

        pieces = ["y" * DEFAULT_CHUNK_SIZE] * DEFAULT_SHINGLE_SIZE
        text = " ".join(pieces)
        segment = _Scripted(vector={"q": [_hit("a", text)]})
        _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        [out] = _replies(scripted_model)
        assert text in out
        assert "…" not in out


class TestJudgedSearches:
    """The default path: a search's hits come back judged, so every hit
    carries a decision and the agent only decides what to search for."""

    def test_a_search_applies_the_judgements_and_leaves_nothing_undecided(
        self, scripted_model
    ):
        segment = _Scripted(
            {"a": (True, "says what one is"), "b": (False, "about two")}, max_chunks=5
        )
        outcome = _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        [out] = _replies(scripted_model)

        assert [r.doc_id for r in outcome.results] == ["a", "c"]
        assert outcome.undecided == 0
        assert "ignored 1." in outcome.note
        # The reply: kept findings in full, one line per ignored one, what is
        # known, and the status.
        assert out.startswith("3 new finding(s) from vector_search: kept 2, ignored 1.")
        assert "Kept:\n\n[f1] A.md\none\n\n[f3] C.md\nthree" in out
        assert "Ignored:\n\n[f2] B.md — about two" in out
        assert "Already known:\n- A.md — says what one is\n- C.md — on topic" in out
        assert "Kept 2 finding(s), at most 5." in out
        assert "enough_information" in out
        assert "keep_finding" not in out
        # The trace lines are the ones a person reads for a hand decision.
        told = " | ".join(segment.told)
        assert "agent · keep f1 (A.md) — says what one is. Kept 1 finding(s)" in told
        assert "agent · ignore f2 (B.md) — about two. Kept 1 finding(s)" in told
        assert "agent ← vector_search: 3 finding(s), kept 2, ignored 1." in told

    def test_the_judge_is_told_the_question_and_looking_for(self, scripted_model):
        segment = _Scripted()
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q", "looking_for": "the other side"}),
            question="what is one?",
        )
        [prompt] = segment.judge.prompts
        assert prompt.startswith("Question: what is one?\n")
        assert "This call is looking for: the other side\n" in prompt
        assert "Already known:\n- nothing yet\n" in prompt

    def test_the_second_search_is_told_what_is_known_and_seen(self, scripted_model):
        segment = _Scripted(
            {"a": (True, "one"), "b": False, "c": (True, "three")},
            vector={"q": THREE, "r": [THREE[0], _hit("d", "four", title="D.md")]},
            max_chunks=5,
        )
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("vector_search", {"query": "r"}),
        )
        assert (
            "Already known:\n- A.md — one\n- C.md — three\n" in segment.judge.prompts[1]
        )
        # "a" was decided by the first search, so only the new hit is judged.
        assert segment.judge.batches == [["A.md", "B.md", "C.md"], ["D.md"]]
        assert _replies(scripted_model)[1].startswith(
            "1 new finding(s) from vector_search: kept 1, ignored 0."
        )
        assert [r.doc_id for r in outcome.results] == ["a", "c", "d"]
        assert outcome.undecided == 0

    def test_a_judgement_on_an_already_decided_hit_is_not_applied_twice(
        self, scripted_model
    ):
        """A judge that ignores `seen` must not re-decide or re-trace a hit."""

        class Forgetful(FindingsJudge):
            def judge(self, findings, question, looking_for="", known=(), seen=()):  # type: ignore[no-untyped-def,override]
                keep = not seen  # keeps first, then changes its mind
                return [
                    Judgement(f, keep, "on topic" if keep else "changed my mind")
                    for f in findings
                ]

        segment = _Scripted(vector={"q": THREE[:1], "again": THREE[:1]})
        segment.judge = Forgetful()
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("vector_search", {"query": "again"}),
        )
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert "ignored 0." in outcome.note
        assert "No new findings from vector_search" in _replies(scripted_model)[1]
        assert not any("changed my mind" in line for line in segment.told)

    def test_keyword_search_is_judged_too(self, scripted_model):
        segment = _Scripted(
            {"b": False}, keyword={"one": THREE[:2]}, index_exists=True, max_chunks=5
        )
        outcome = _run(
            scripted_model,
            segment,
            ("keyword_search", {"query": "one", "looking_for": "two"}),
        )
        assert "This call is looking for: two" in segment.judge.prompts[0]
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert "ignored 1." in outcome.note
        assert "kept 1, ignored 1" in _replies(scripted_model)[0]

    def test_the_chunk_maximum_is_respected_in_rank_order(self, scripted_model):
        segment = _Scripted(max_chunks=2)
        outcome = _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        [out] = _replies(scripted_model)

        assert [r.doc_id for r in outcome.results] == ["a", "b"]
        # The third was judged worth keeping but not registered, so it is
        # neither kept nor left undecided -- and the agent is told.
        assert outcome.undecided == 0
        assert "1 more relevant finding(s) were not kept: the 2-finding maximum" in out
        assert "Stop calling tools and reply: done" in out
        assert any(
            "3 finding(s), kept 2, ignored 0, 1 passed over." in line
            for line in segment.told
        )
        assert outcome.note.startswith("Agent filled its 2-chunk maximum")

    def test_everything_ignored_keeps_nothing_and_says_so(self, scripted_model):
        segment = _Scripted({"a": False, "b": False, "c": False})
        outcome = _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        [out] = _replies(scripted_model)
        assert outcome.results == []
        assert "ignored 3." in outcome.note
        assert "kept 0, ignored 3" in out
        assert "Already known: nothing yet." in out
        assert "Kept:" not in out

    def test_a_search_with_no_hits_says_so(self, scripted_model):
        segment = _Scripted(vector={})
        _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        assert "No new findings from vector_search" in _replies(scripted_model)[0]
        assert any("0 finding(s), kept 0, ignored 0" in x for x in segment.told)
        assert segment.judge.batches == []  # nothing to judge, no model call

    def test_a_failed_judgement_keeps_the_hits_as_not_judged(self, scripted_model):
        segment = _Scripted(vector={"q": THREE}, max_chunks=5)
        segment.judge = ScriptedJudge(error=RuntimeError("judge down"))
        outcome = _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        assert [r.doc_id for r in outcome.results] == ["a", "b", "c"]
        assert "not judged: judge down" in _replies(scripted_model)[0]

    def test_read_document_parts_are_judged(self, scripted_model):
        hits = {"q": [_hit("row1", "a chunk", title="guide.md", source="/d/guide.md")]}
        segment = _Scripted(
            {"/d/guide.md": (True, "the full procedure")},
            vector=hits,
            read=lambda source: "The whole document.",
            max_chunks=5,
        )
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("read_document", {"finding_id": "f1", "looking_for": "the steps"}),
        )
        out = _replies(scripted_model)[1]
        assert out.startswith("1 new finding(s) from read_document: kept 1, ignored 0.")
        assert "[f2] guide.md, characters 0-19 of 19\nThe whole document." in out
        assert "Kept 2 finding(s), at most 5." in out
        assert "keep_finding" not in out
        assert [r.doc_id for r in outcome.results] == ["row1", "/d/guide.md"]
        prompt = segment.judge.prompts[-1]
        assert "This call is looking for: the steps" in prompt
        assert "Already known:\n- guide.md — on topic\n" in prompt
        assert "agent · keep f2 (guide.md, characters 0-19 of 19) — the full" in (
            " ".join(segment.told)
        )

    def test_a_document_part_judged_irrelevant_is_ignored(self, scripted_model):
        hits = {"q": [_hit("row1", "a chunk", title="guide.md", source="/d/guide.md")]}
        segment = _Scripted(
            {"/d/guide.md": (False, "nothing new")},
            vector=hits,
            read=lambda source: "The whole document.",
            max_chunks=5,
        )
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("read_document", {"finding_id": "f1"}),
            ("read_document", {"finding_id": "f1"}),
        )
        _, first, again = _replies(scripted_model)
        assert "[f2] guide.md, characters 0-19 of 19 — nothing new" in first
        assert "ignored 1." in outcome.note
        # Reading the same part again does not judge it again.
        assert "No new findings from read_document" in again
        assert len(segment.judge.batches) == 2

    def test_a_document_part_judged_relevant_at_capacity_is_not_kept(self):
        """The guard refuses the tool once the maximum is reached, so drive
        the loop directly to exercise the judgement itself."""
        hit = _hit("row1", "a chunk", title="g.md", source="/d/g.md")
        session = FindingsSession("q", max_kept=1)
        loop = JudgeFindings(session, ScriptedJudge(keep=lambda f: True))
        loop.process(search_findings([hit]), tool="vector_search")
        assert session.at_capacity
        part = agent_tools.read_part(
            SearchBackend(read_document=lambda s: "More."), session.kept[0], "f1", None
        )
        out = loop.process([part], tool="read_document")
        assert "1 more relevant finding(s) were not kept" in out
        assert session.kept_ids == ["f1"]
        assert session.undecided == []

    def test_a_failed_judgement_of_a_part_keeps_it_as_not_judged(self, scripted_model):
        hits = {"q": [_hit("row1", "a chunk", title="g.md", source="/d/g.md")]}
        segment = _Scripted(vector=hits, read=lambda s: "More.", max_chunks=5)
        scripted_model.call("vector_search", query="q")

        def fail_after_the_search(finding: Any) -> bool:
            if finding.source == "/d/g.md" and finding.text == "More.":
                raise RuntimeError("judge down")
            return True

        segment.judge = ScriptedJudge(keep=fail_after_the_search)
        outcome = _run(scripted_model, segment, ("read_document", {"finding_id": "f1"}))
        assert "not judged: judge down" in _replies(scripted_model)[1]
        assert [r.doc_id for r in outcome.results] == ["row1", "/d/g.md"]


class TestReadDocument:
    """read_document loads the document behind a finding the agent has
    seen, as a new finding that is kept or ignored like a search hit (by
    the agent, when the session has no judge)."""

    TOOLS = "vector_search,read_document,keep_chunk,ignore_chunk,enough_information"

    def _segment(self, text: str = "The whole document.", **kwargs: Any):
        hits = {"q": [_hit("row1", "a chunk", title="guide.md", source="/d/guide.md")]}
        read: list[str] = []

        def reader(source: str) -> str:
            read.append(source)
            return text

        kwargs.setdefault("read", reader)
        segment = _Scripted(
            judge=False, vector=hits, tools=self.TOOLS, max_chunks=5, **kwargs
        )
        return segment, read

    def test_the_document_becomes_a_keepable_finding(self, scripted_model):
        segment, read = self._segment()
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("read_document", {"finding_id": "f1"}),
            ("keep_finding", {"finding_id": "f2", "reason": "the full procedure"}),
        )
        out = _replies(scripted_model)[1]
        assert read == ["/d/guide.md"]
        assert "[f2] guide.md, characters 0-19 of 19\nThe whole document." in out
        assert "keep_finding or ignore_finding" in out
        assert any("read_document" in line for line in segment.told)

        [kept] = outcome.results
        assert kept.document["content"] == "The whole document."
        assert kept.document["title"] == "guide.md"
        # The source, not the chunk's row id, so the document can be opened
        # from the citation.
        assert kept.doc_id == "/d/guide.md"

    def test_a_long_document_is_read_in_parts(self, scripted_model):
        size = agent_tools.DOCUMENT_PART_CHARS
        segment, _ = self._segment("a" * size + "b" * 10)
        with _tool_errors():
            outcome = _run(
                scripted_model,
                segment,
                ("vector_search", {"query": "q"}),
                ("read_document", {"finding_id": "f1"}),
                ("read_document", {"finding_id": "f1", "start": size}),
                ("read_document", {"finding_id": "f1", "start": size + 10}),
                ("keep_finding", {"finding_id": "f2"}),
                ("keep_finding", {"finding_id": "f3"}),
            )
        _, first, second, beyond, *_ = _replies(scripted_model)
        # The label says where the part ends, which is where to read on.
        assert f"characters 0-{size} of {size + 10}" in first
        assert "b" not in first.split("\n\n", 1)[1]
        assert f"characters {size}-{size + 10} of {size + 10}" in second
        assert "b" * 10 in second
        assert "nothing from character" in beyond
        first_part, second_part = outcome.results
        assert first_part.document["content"] == "a" * size
        assert second_part.document["content"] == "b" * 10

    def test_rereading_a_part_returns_the_same_finding(self, scripted_model):
        segment, _ = self._segment()
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("read_document", {"finding_id": "f1"}),
            ("read_document", {"finding_id": "f1"}),
        )
        _, first, again = _replies(scripted_model)
        assert "[f2]" in first
        assert "[f2]" in again
        assert "f3" not in again

    def test_an_unknown_finding_id_is_refused_and_traced(self, scripted_model):
        segment, read = self._segment()
        with _tool_errors():
            _run(
                scripted_model,
                segment,
                ("vector_search", {"query": "q"}),
                ("read_document", {"finding_id": "f9"}),
            )
        assert "Unknown finding id 'f9'" in _replies(scripted_model)[1]
        assert read == []
        assert any(
            line.startswith("agent ← read_document: error: Unknown finding id 'f9'")
            for line in segment.told
        )

    def test_outside_a_session_there_is_nothing_to_read(self):
        [tool] = build_tools(SearchBackend(), ["read_document"])
        with pytest.raises(ValueError, match="Unknown finding id"):
            tool(finding_id="f1")

    def test_without_a_reader_the_tool_says_so(self):
        [finding] = search_findings([_hit("a", "x", source="/d/a.md")])
        with pytest.raises(ValueError, match="unavailable"):
            agent_tools.read_part(SearchBackend(), finding, "f1", None)

    def test_a_finding_without_a_source_cannot_be_read(self):
        [finding] = search_findings([_hit("a", "x")])
        backend = SearchBackend(read_document=lambda s: "text")
        with pytest.raises(ValueError, match="no source document"):
            agent_tools.read_part(backend, finding, "f1", None)

    def test_a_failed_read_is_reported_to_the_agent(self, scripted_model):
        def fail(source: str) -> str:
            raise FileNotFoundError("guide.md is gone")

        segment, _ = self._segment(read=fail)
        with _tool_errors():
            _run(
                scripted_model,
                segment,
                ("vector_search", {"query": "q"}),
                ("read_document", {"finding_id": "f1"}),
            )
        assert "guide.md is gone" in _replies(scripted_model)[1]
        assert any(
            line.startswith("agent ← read_document: error:")
            and line.endswith("guide.md is gone")
            for line in segment.told
        )


class TestAgentIsAnLLMAgent:
    """The agent is talkpipe-agents' llmAgent segment running its findings
    loop, configured by this module, not a Strands agent assembled here."""

    def test_make_agent_configures_llmagent_from_the_segment(self):
        from talkpipe_agents import LLMAgent

        segment = _Scripted(max_iterations=5)
        session = segment.make_session("q")
        agent = segment.make_agent(session)

        assert isinstance(agent, LLMAgent)
        assert agent.model == "any"
        assert agent.source == "scripted"
        assert agent.system_prompt == agent_search.RETRIEVAL_SYSTEM_PROMPT
        # The iteration cap is the library's turn limit, not a second loop.
        assert agent.max_turns == 5
        assert {t.tool_name for t in agent.tools} == set(
            agent_tools.default_tool_names()
        )
        # The findings loop's hook, over this query's session and the judge.
        [loop] = agent.hooks
        assert isinstance(loop, JudgeFindings)
        assert loop.session is session
        assert loop.judge is segment.judge

    def test_with_max_iterations_raises_the_cap_on_a_copy(self):
        """Extra Effort: same bindings, more turns, original untouched."""
        segment = _Scripted(max_iterations=5)
        extra = segment.with_max_iterations(10)

        assert extra is not segment
        assert segment.max_iterations == 5
        assert extra.make_agent(extra.make_session()).max_turns == 10
        assert extra._extract_keywords is segment._extract_keywords
        assert extra.agent_tools is segment.agent_tools
        assert segment.with_max_iterations(0).max_iterations == 1

    def test_the_tools_parameter_selects_what_the_agent_gets(self):
        segment = _Scripted(tools="vector_search,think")
        agent = segment.make_agent(segment.make_session())
        assert {t.tool_name for t in agent.tools} == {"vector_search", "think"}

    def test_the_system_prompt_is_replaceable(self):
        segment = _Scripted()
        segment.system_prompt = "Be brief."
        assert segment.make_agent(segment.make_session()).system_prompt == "Be brief."

    def test_the_default_prompt_describes_the_judged_tools(self):
        prompt = agent_search.RETRIEVAL_SYSTEM_PROMPT
        for name in agent_tools.default_tool_names():
            assert name in prompt
        assert "looking_for" in prompt
        assert "keep_finding" not in prompt
        assert "ignore_finding" not in prompt
        assert "ordinary sense" in prompt
        assert "how many are left" in prompt
        assert "nothing at all has been kept" in prompt

    def test_progress_reaches_the_session_trace(self):
        """llmAgent's own progress lines land in the trace the pages show."""
        segment = _Scripted()
        session = segment.make_session()
        segment.make_agent(session).tell("agent → vector_search({'query': 'q'})")
        assert session.trace == ["agent → vector_search({'query': 'q'})"]
        assert segment.told == ["agent → vector_search({'query': 'q'})"]

    def test_the_session_is_built_for_the_question(self):
        segment = _Scripted()
        assert segment.make_session().question == ""
        session = segment.make_session("what is one?")
        assert session.question == "what is one?"
        assert session.max_kept == segment.max_chunks
        # The agent only gathers, so the run ends with "done", not an answer.
        assert session.stop_instruction == agent_search.STOP_INSTRUCTION

    def test_the_session_knows_the_turn_cap(self):
        """The iteration cap reaches the session, so the replies can count
        down; the Extra Effort copy passes its own."""
        segment = _Scripted(max_iterations=5)
        assert segment.make_session("q").max_turns == 5
        assert segment.with_max_iterations(10).make_session("q").max_turns == 10


class TestTurnsLeft:
    """Every tool reply says how many turns the agent has left, counted
    from the agent running the tools, so the model knows when to stop
    searching and call enough_information."""

    def test_replies_count_down_and_announce_the_last_turn(self, scripted_model):
        segment = _Scripted(
            vector={"q": THREE[:1], "r": THREE[1:2]}, max_chunks=9, max_iterations=3
        )
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("vector_search", {"query": "r"}),
        )
        first, second = _replies(scripted_model)
        assert "2 of 3 turns left." in first
        assert "1 turn left: the next one is your last" in second
        assert "call enough_information now" in second
        # The status line carries it, so the trace shows it too.
        assert any(
            line.endswith(
                "1 turn left: the next one is your last, so call "
                "enough_information now rather than another "
                "tool."
            )
            for line in segment.told
        )

    def test_the_last_turn_with_nothing_kept_says_to_try_once_more(
        self, scripted_model
    ):
        """enough_information is refused with nothing kept, so the last-turn
        line must not send the model to it."""
        segment = _Scripted({"a": False, "b": False, "c": False}, max_iterations=2)
        _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        [out] = _replies(scripted_model)
        assert "1 turn left: the next one is your last, and nothing is kept yet" in out
        assert "call enough_information now" not in out

    def test_every_tool_reports_the_turn(self, scripted_model):
        """A reply with no status line of its own still ends with the count,
        including the plain extract_keywords tool."""
        segment = _Scripted(max_iterations=8, max_chunks=5)
        _run(
            scripted_model,
            segment,
            ("extract_keywords", {"question": "q"}),
            ("vector_search", {"query": "q"}),
            ("enough_information", {"reason": "done"}),
            ("vector_search", {"query": "again"}),
        )
        keywords, search, enough, refused = _replies(scripted_model)
        assert keywords.endswith("7 of 8 turns left.")
        assert "6 of 8 turns left." in search
        assert "done" in enough
        # A refusal carries it too, and the note appears once, not twice.
        assert refused.count("turns left") == 1

    def test_the_context_parameter_is_hidden_from_the_model(self):
        for tool in build_tools(SearchBackend(), agent_tools.vault_agent_tool_names()):
            schema = tool.tool_spec["inputSchema"]["json"]["properties"]
            assert "tool_context" not in schema, tool.tool_name


class TestAgentVaultSearch:
    def test_outcome_reports_the_agents_own_judgement(self, scripted_model):
        """The intended ending: the agent said it could answer, after one search."""
        segment = _Scripted({"a": (True, "one"), "b": False, "c": False}, max_chunks=5)
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "what is one?"}),
            ("enough_information", {"reason": "f1 answers it"}),
            question="what is one?",
        )
        assert isinstance(outcome, AgentSearchOutcome)
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert outcome.note.startswith("Agent judged it had enough to answer")
        # The reason it gave is carried into the note the pages display.
        assert "(f1 answers it)" in outcome.note
        assert "kept 1 chunk after 1 search" in outcome.note
        # The judge decided on every hit, so nothing is undecided.
        assert "ignored 2." in outcome.note
        assert outcome.undecided == 0
        assert outcome.trace[0].startswith("agent search: 'what is one?'")
        assert outcome.trace == segment.told
        # The judge was asked about the user's question.
        assert segment.judge.prompts[0].startswith("Question: what is one?\n")

    def test_outcome_reports_the_chunk_ceiling(self, scripted_model):
        segment = _Scripted({"b": False}, max_chunks=2)
        outcome = _run(scripted_model, segment, ("vector_search", {"query": "q"}))
        assert [r.doc_id for r in outcome.results] == ["a", "c"]
        assert outcome.note.startswith("Agent filled its 2-chunk maximum")
        assert "kept 2 chunks after 1 search" in outcome.note
        assert "ignored 1." in outcome.note
        assert outcome.undecided == 0

    def test_outcome_reports_iteration_cap(self, scripted_model):
        segment = _Scripted(
            vector={"q": THREE[:1], "r": THREE[1:2], "s": THREE[2:]},
            max_chunks=5,
            max_iterations=2,
        )
        for query in "qrs":
            scripted_model.call("vector_search", query=query)
        outcome = segment.process_value("q")
        assert [r.doc_id for r in outcome.results] == ["a", "b"]
        assert "hit the 2-iteration cap" in outcome.note

    def test_outcome_distinguishes_stopping_without_deciding(self, scripted_model):
        """A model that stops without judging is not the same as one that did."""
        outcome = _run(
            scripted_model, _Scripted(max_chunks=5), ("vector_search", {"query": "q"})
        )
        assert outcome.note.startswith("Agent stopped without saying it had enough")
        # ...and says that the model is the likely cause.
        assert agent_search.WEAK_TOOL_CALLING_HINT in outcome.note
        # A model that never searched at all ends the same way.
        outcome = _run(scripted_model, _Scripted())
        assert outcome.note.startswith("Agent stopped without saying it had enough")
        assert agent_search.WEAK_TOOL_CALLING_HINT in outcome.note

    def test_nothing_relevant_is_not_blamed_on_the_model(self, scripted_model):
        """Searching, having every hit judged irrelevant, and stopping is the
        right ending for a question the notes do not cover -- not a sign of
        weak tool calling."""
        segment = _Scripted({"a": False, "b": False, "c": False})
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("vector_search", {"query": "r"}),
        )
        assert outcome.results == []
        assert outcome.note == (
            "Agent found nothing relevant to keep: kept 0 chunks after 2 "
            "searches, ignored 3."
        )
        assert agent_search.WEAK_TOOL_CALLING_HINT not in outcome.note
        assert outcome.undecided == 0

    def test_a_second_search_builds_on_the_first(self, scripted_model):
        """What the first search kept is `known` and `seen` to the second."""
        segment = _Scripted(
            {"a": (True, "one side"), "b": False},
            vector={"q": THREE, "r": [*THREE, _hit("d", "four", title="D.md")]},
            max_chunks=5,
        )
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("vector_search", {"query": "r", "looking_for": "the other side"}),
            ("enough_information", {}),
        )
        first, second = segment.judge.prompts
        assert "looking for" not in first
        assert "This call is looking for: the other side" in second
        assert "Already known:\n- A.md — one side\n- C.md — on topic\n" in second
        # Only what the first search did not decide is judged again.
        assert segment.judge.batches == [["A.md", "B.md", "C.md"], ["D.md"]]

    def test_agent_failure_keeps_what_was_selected(self, scripted_model):
        scripted_model.call("vector_search", query="q").call("no_such_tool")
        outcome = _Scripted({"a": False, "c": False}).process_value("q")
        assert [r.doc_id for r in outcome.results] == ["b"]
        assert "no_such_tool" in outcome.note
        assert outcome.note.endswith("showing what it kept.")
        assert "no_such_tool" in outcome.error

    def test_agent_failure_before_keeping_anything_says_so(self, scripted_model):
        scripted_model.call("no_such_tool")
        outcome = _Scripted().process_value("q")
        assert outcome.results == []
        assert outcome.note.startswith("The agent stopped with an error (")
        assert outcome.note.endswith("it kept nothing.")
        assert "no_such_tool" in outcome.error

    def test_a_run_that_did_not_fail_has_no_error(self, scripted_model):
        outcome = _run(scripted_model, _Scripted(), ("vector_search", {"query": "q"}))
        assert outcome.error == ""

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
        segment = _Scripted()
        assert "All of it." in segment.read_document(str(doc))
        # A document that moved since indexing is reported, not created.
        with pytest.raises(FileNotFoundError, match=r"gone\.md"):
            segment.read_document(str(tmp_path / "gone.md"))

    def test_as_function_yields_the_outcome(self, scripted_model):
        scripted_model.call("vector_search", query="q").say("done")
        fn = _Scripted(max_chunks=1).as_function(single_in=True, single_out=True)
        assert [r.doc_id for r in fn("q").results] == ["a"]


class _ScriptedChat(_Scripted, agent_search.AgentVaultChat):
    """The scripted agent with the RAG answering stage stubbed out, so the
    Ask variant is exercised without a vault, an embedder, or an LLM."""

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _Scripted.__init__(self, *args, **kwargs)
        self.answered: list[tuple[str, list[Any]]] = []

    def answer(self, question: str, background: list[Any]) -> str:
        self.answered.append((question, background))
        return f"Answer to {question} from {len(background)} chunk(s)."


class TestAgentVaultChat:
    def test_answer_is_built_from_the_chunks_the_agent_kept(self, scripted_model):
        segment = _ScriptedChat({"b": False}, max_chunks=5)
        outcome = _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "what is one?"}),
            ("enough_information", {"reason": "both sides covered"}),
            question="what is one?",
        )

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

    def test_everything_judged_irrelevant_means_no_prompt_is_sent(self, scripted_model):
        """With every hit judged, "nothing relevant" is accurate: the answer
        says the notes do not cover the question, not that the agent never
        decided."""
        segment = _ScriptedChat({"a": False, "b": False, "c": False}, max_iterations=2)
        for query in ("q", "q again", "q once more"):
            scripted_model.call("vector_search", query=query)
        outcome = segment.process_value("q")

        assert segment.answered == []
        assert outcome.results == []
        assert outcome.response == agent_search.NO_CONTEXT_ANSWER
        assert outcome.error == ""
        assert outcome.undecided == 0
        assert outcome.note.startswith("Agent hit the 2-iteration cap")
        assert "ignored 3." in outcome.note
        assert not hasattr(agent_search, "UNDECIDED_ANSWER")

    def test_a_failed_run_is_not_answered_as_an_empty_vault(self, scripted_model):
        """An agent that errored before searching never looked at the vault;
        the answer must not blame the vault or the question."""
        scripted_model.call("no_such_tool")
        segment = _ScriptedChat()
        outcome = segment.process_value("q")

        assert segment.answered == []
        assert outcome.response == agent_search.AGENT_ERROR_ANSWER
        assert "no_such_tool" in outcome.error
        assert "no_such_tool" in outcome.note

    def test_as_function_yields_the_outcome(self, scripted_model):
        scripted_model.call("vector_search", query="q").say("done")
        fn = _ScriptedChat(max_chunks=1).as_function(single_in=True, single_out=True)
        outcome = fn("q")
        assert outcome.response.startswith("Answer to q")
        assert [r.doc_id for r in outcome.results] == ["a"]

    def test_select_chunks_answers_nothing(self, scripted_model):
        """The Search page drives the same segment through select_chunks."""
        scripted_model.call("vector_search", query="q").say("done")
        segment = _ScriptedChat(max_chunks=1)
        outcome = segment.select_chunks("q")

        assert isinstance(outcome, AgentSearchOutcome)
        assert not isinstance(outcome, agent_search.AgentChatOutcome)
        assert [r.doc_id for r in outcome.results] == ["a"]
        assert segment.answered == []


class TestToolsRefuseOnceTheRunIsOver:
    """The stopping rule is enforced by the tools themselves (through the
    session), so it holds behind any agent."""

    def test_tools_refuse_after_the_agent_says_it_has_enough(self, scripted_model):
        segment = _Scripted(vector={"q": [_hit("a", "one")]}, max_chunks=5)
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("enough_information", {"reason": "that covers it"}),
            ("vector_search", {"query": "more"}),
            ("extract_keywords", {"question": "more"}),
        )
        *_, search, keywords = _replies(scripted_model)
        for out in (search, keywords):
            assert "you already said you had enough" in out
            assert "done" in out
        assert "refused: you already said you had enough" in segment.told[-2]
        # ...and the refused call really did nothing.
        assert len(segment.judge.batches) == 1

    def test_tools_refuse_once_the_chunk_ceiling_is_full(self, scripted_model):
        segment = _Scripted(max_chunks=1)
        _run(
            scripted_model,
            segment,
            ("vector_search", {"query": "q"}),
            ("vector_search", {"query": "more"}),
        )
        assert "1-finding maximum is reached" in _replies(scripted_model)[1]
        assert any(
            "refused: the 1-finding maximum is reached" in x for x in segment.told
        )
        assert len(segment.judge.batches) == 1


class TestToolSelection:
    """Tools are picked by name, so a pipeline can add or drop one without
    touching the tool pack or the segment."""

    def _names(self, spec: Any) -> set[str]:
        tools = build_tools(SearchBackend(), agent_tools.parse_tool_names(spec))
        return {t.tool_name for t in tools}

    def test_the_default_pack_is_the_five_judged_search_tools(self):
        assert agent_tools.default_tool_names() == [
            "vector_search",
            "keyword_search",
            "extract_keywords",
            "read_document",
            "enough_information",
        ]
        assert self._names(None) == set(agent_tools.default_tool_names())
        assert agent_tools.parse_tool_names(None) == agent_tools.default_tool_names()

    def test_keep_and_ignore_are_opt_ins_under_their_old_names_too(self):
        assert agent_tools.vault_agent_tool_names() == [
            "vector_search",
            "keyword_search",
            "extract_keywords",
            "read_document",
        ]
        assert self._names("vector_search,keep_chunk,ignore_chunk") == {
            "vector_search",
            "keep_finding",
            "ignore_finding",
        }
        assert self._names("keep_finding,ignore_finding") == {
            "keep_finding",
            "ignore_finding",
        }

    @pytest.mark.parametrize(
        "spec", ["vector_search,keep_finding", ["vector_search", "keep_finding"]]
    )
    def test_a_named_subset_binds_only_those_tools(self, spec):
        assert self._names(spec) == {"vector_search", "keep_finding"}

    def test_a_generic_talkpipe_agents_tool_can_be_mixed_in(self):
        """Names the vault does not own fall through to the shared registry."""
        assert self._names("vector_search,think") == {"vector_search", "think"}

    def test_an_unknown_tool_name_is_refused_with_both_registries_listed(self):
        with pytest.raises(ValueError) as excinfo:
            build_tools(SearchBackend(), ["vector_search", "no_such_tool"])
        message = str(excinfo.value)
        assert "no_such_tool" in message
        assert "vault retrieval tools" in message
        assert "read_document" in message
        assert "enough_information" in message
        assert "keep_finding" in message

    def test_a_blank_spec_falls_back_to_the_default_pack(self):
        assert agent_tools.parse_tool_names("") == agent_tools.default_tool_names()
        assert agent_tools.parse_tool_names([]) == agent_tools.default_tool_names()


@pytest.mark.parametrize("bad", [0, -3])
def test_the_chunk_ceiling_has_a_floor(bad):
    segment = _Scripted(max_chunks=bad)
    assert segment.max_chunks == 1
    assert segment.make_session("q").max_kept == 1
