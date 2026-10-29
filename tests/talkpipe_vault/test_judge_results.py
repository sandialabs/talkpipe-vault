"""Tests for the judgeSearchResults segment (pipelines/judge_results.py), a
thin segment over talkpipe-agents' FindingsJudge.

The model call is stubbed: a scripted judge decides by result label, and a
throwaway prompt adapter stands in for a provider so the segment also runs
from a compiled ChatterLang script. Nothing here needs a model or a vault.
"""

from __future__ import annotations

from typing import Any

import pytest
import talkpipe
from talkpipe.search.abstract import SearchResult

from talkpipe_vault.pipelines import folder_index
from talkpipe_vault.pipelines.judge_results import (
    JUDGE_SYSTEM_PROMPT,
    Decision,
    Judgements,
    JudgeSearchResults,
)
from talkpipe_vault.pipelines.vault_search import VaultSearch
from tests.conftest import FAKE_EMBEDDING_SOURCE

FAKE_CHAT_SOURCE = "fakejudge"


def _hit(doc_id: str, content: str, **doc: Any) -> SearchResult:
    return SearchResult(score=0.5, doc_id=doc_id, document={"content": content, **doc})


@pytest.fixture
def fake_judge():
    """Register a throwaway chat provider whose structured replies are scripted.

    Yields a controller: ``decide`` maps a result label (``f1``, …) to a
    ``(keep, reason)`` pair, or a callable taking the prompt and returning
    the ``Judgements`` to reply with (or raising); ``prompts`` collects what
    the judge was asked, ``system_prompts`` how it was configured.
    """
    from talkpipe.llm import config as llm_config

    control: dict[str, Any] = {"decide": {}, "prompts": [], "system_prompts": []}

    class _FakeJudgeAdapter:
        def __init__(self, model=None, system_prompt=None, output_format=None, **_):
            self.model = model
            self.output_format = output_format
            control["system_prompts"].append(system_prompt)

        def execute(self, prompt):
            control["prompts"].append(prompt)
            decide = control["decide"]
            if callable(decide):
                return decide(prompt)
            return Judgements(
                decisions=[
                    Decision(id=label, keep=keep, reason=reason)
                    for label, (keep, reason) in decide.items()
                ]
            )

    llm_config.registerPromptAdapter(FAKE_CHAT_SOURCE, _FakeJudgeAdapter)
    try:
        yield control
    finally:
        llm_config._promptAdapter.pop(FAKE_CHAT_SOURCE, None)


def _segment(**kwargs: Any) -> JudgeSearchResults:
    return JudgeSearchResults(chat_source=FAKE_CHAT_SOURCE, chat_model="m", **kwargs)


HITS = [
    _hit("a", "Poems as adversarial prompts.", title="poetry.md"),
    _hit("b", "Installing the Poetry package manager.", title="poetry-pm.md"),
    _hit("c", "Verse and jailbreaks, continued.", title="poetry.md"),
]


class TestJudge:
    def test_decisions_are_applied_in_rank_order(self, fake_judge):
        fake_judge["decide"] = {
            "f3": (True, "more on verse attacks"),
            "f1": (True, "poems as attacks"),
            "f2": (False, "the package manager"),
        }
        judged = _segment().judge(HITS, "poetry and LLM security")
        assert [(j["result"].doc_id, j["keep"]) for j in judged] == [
            ("a", True),
            ("b", False),
            ("c", True),
        ]
        assert [j["reason"] for j in judged] == [
            "poems as attacks",
            "the package manager",
            "more on verse attacks",
        ]
        # One model call judged the whole list.
        assert len(fake_judge["prompts"]) == 1

    def test_labels_written_differently_still_match(self, fake_judge):
        # "r1" is how this segment labelled results before it used the
        # generic judge's "f1"; models that write either are understood.
        fake_judge["decide"] = {
            "1": (False, "x"),
            "R2": (False, "y"),
            "[r3]": (False, "z"),
        }
        judged = _segment().judge(HITS, "q")
        assert [j["keep"] for j in judged] == [False, False, False]
        assert [j["reason"] for j in judged] == ["x", "y", "z"]

    def test_a_missing_or_unknown_id_keeps_the_result_as_not_judged(self, fake_judge):
        fake_judge["decide"] = {"f1": (False, "no"), "f9": (True, "nonsense")}
        judged = _segment().judge(HITS, "q")
        assert [j["keep"] for j in judged] == [False, True, True]
        assert judged[1]["reason"].startswith("not judged")
        assert judged[2]["reason"].startswith("not judged")

    def test_a_model_failure_keeps_everything(self, fake_judge):
        def boom(_prompt: str) -> Judgements:
            raise RuntimeError("model down")

        fake_judge["decide"] = boom
        judged = _segment().judge(HITS, "q")
        assert [j["keep"] for j in judged] == [True, True, True]
        assert all(j["reason"] == "not judged: model down" for j in judged)

    def test_an_unparseable_reply_keeps_everything(self, fake_judge):
        fake_judge["decide"] = lambda _prompt: "I cannot decide."
        judged = _segment().judge(HITS, "q")
        assert [j["keep"] for j in judged] == [True, True, True]
        assert all(
            j["reason"] == "not judged: the judge's reply held no decisions"
            for j in judged
        )

    def test_a_failure_reason_is_one_short_line(self, fake_judge):
        def boom(_prompt: str) -> str:
            raise RuntimeError("line one\n   line two " + "x" * 300)

        fake_judge["decide"] = boom
        [judged, *_] = _segment().judge(HITS, "q")
        assert "\n" not in judged["reason"]
        assert judged["reason"].startswith("not judged: line one line two xxx")
        assert len(judged["reason"]) < 200

    JSON_REPLY = (
        '{"decisions": [{"id": "r1", "keep": true, "reason": "yes"}, '
        '{"id": "r2", "keep": false, "reason": "no"}]}'
    )

    @pytest.mark.parametrize(
        "reply",
        [
            JSON_REPLY,
            f"Here you go:\n```json\n{JSON_REPLY}\n```",
            JSON_REPLY[len('{"decisions": ') : -1],  # the bare list
            "r1: true – yes\nr2: false – no",  # noqa: RUF001 - en dashes, as gpt-oss writes
            "[r1] KEEP — yes.\n[r2] ignore: no",
            "1 keep yes\n2 ignore no\nThat is all.",
        ],
    )
    def test_replies_in_json_or_prose_are_both_read(self, fake_judge, reply):
        """The prompt asks for JSON; models that answer in prose (Ollama's
        cloud models ignore its format schema) are read line by line."""
        fake_judge["decide"] = lambda _prompt: reply
        judged = _segment().judge(HITS[:2], "q")
        assert [(j["keep"], j["reason"]) for j in judged] == [
            (True, "yes"),
            (False, "no"),
        ]

    def test_seen_results_are_skipped(self, fake_judge):
        fake_judge["decide"] = {"f1": (True, "x"), "f2": (True, "y")}
        judged = _segment().judge(HITS, "q", seen=["b"])
        assert [j["result"].doc_id for j in judged] == ["a", "c"]
        prompt = fake_judge["prompts"][0]
        # They are not shown to the judge either: the labels run f1, f2.
        assert "[f1] poetry.md" in prompt
        assert "[f2] poetry.md" in prompt
        assert "f3" not in prompt
        assert "package manager" not in prompt

    def test_nothing_to_judge_makes_no_call(self, fake_judge):
        assert _segment().judge([], "q") == []
        assert _segment().judge(HITS, "q", seen=["a", "b", "c"]) == []
        assert fake_judge["prompts"] == []

    def test_the_prompt_carries_question_looking_for_and_known(self, fake_judge):
        fake_judge["decide"] = {}
        _segment().judge(
            HITS[:1],
            "poetry and LLM security",
            looking_for="defences, not attacks",
            known=["poetry.md — poems as attacks", ""],
        )
        [prompt] = fake_judge["prompts"]
        assert prompt.startswith("Question: poetry and LLM security\n")
        assert "This call is looking for: defences, not attacks\n" in prompt
        assert "Already known:\n- poetry.md — poems as attacks\nFindings:" in prompt
        assert "[f1] poetry.md\nPoems as adversarial prompts." in prompt
        assert fake_judge["system_prompts"] == [JUDGE_SYSTEM_PROMPT]

    def test_the_prompt_without_looking_for_or_known(self, fake_judge):
        fake_judge["decide"] = {}
        _segment().judge(HITS[:1], "q")
        [prompt] = fake_judge["prompts"]
        assert "looking for" not in prompt
        assert "Already known:\n- nothing yet\n" in prompt

    def test_result_text_is_capped(self, fake_judge):
        fake_judge["decide"] = {}
        _segment(max_chars=10).judge([_hit("a", "x" * 50)], "q")
        assert "x" * 10 + "…" in fake_judge["prompts"][0]
        assert "x" * 11 not in fake_judge["prompts"][0]

    def test_dict_hits_are_normalized_and_renamed(self, fake_judge):
        fake_judge["decide"] = {"f1": (True, "ok")}
        hit = {
            "doc_id": "k",
            "score": 1.0,
            "document": {"content": "text", "filename": "k.md", "path": "/d/k.md"},
        }
        [judged] = _segment(rename_fields="path:source,filename:title").judge(
            [hit], "q"
        )
        assert isinstance(judged["result"], SearchResult)
        assert judged["result"].document["title"] == "k.md"
        assert judged["result"].document["source"] == "/d/k.md"


class TestSegment:
    def test_judgements_are_set_on_the_item(self, fake_judge):
        fake_judge["decide"] = {"f1": (True, "yes"), "f2": (False, "no")}
        segment = _segment(set_as="verdicts")
        [item] = list(segment([{"results": HITS[:2], "question": "q"}]))
        assert [j["keep"] for j in item["verdicts"]] == [True, False]
        assert item["results"] == HITS[:2]

    def test_drop_ignored_sets_only_the_kept_results(self, fake_judge):
        fake_judge["decide"] = {
            "f1": (True, "yes"),
            "f2": (False, "no"),
            "f3": (True, "yes"),
        }
        [item] = list(
            _segment(drop_ignored=True, set_as="results")(
                [{"results": HITS, "question": "q"}]
            )
        )
        assert [r.doc_id for r in item["results"]] == ["a", "c"]
        assert all(isinstance(r, SearchResult) for r in item["results"])

    def test_the_fields_are_configurable_and_optional(self, fake_judge):
        fake_judge["decide"] = {"f1": (True, "yes")}
        segment = _segment(
            results_field="hits",
            question_field="q",
            looking_for_field="want",
            known_field="notes",
            seen_field="done",
        )
        item = {
            "hits": HITS,
            "q": "the question",
            "want": "the part",
            "notes": ["one"],
            "done": ["b", "c"],
        }
        [out] = list(segment([item]))
        assert [j["result"].doc_id for j in out["judged"]] == ["a"]
        [prompt] = fake_judge["prompts"]
        assert "Question: the question" in prompt
        assert "looking for: the part" in prompt
        assert "- one" in prompt
        # Every optional field may be absent.
        [out] = list(segment([{"hits": HITS[:1], "q": "the question"}]))
        assert [j["keep"] for j in out["judged"]] == [True]

    def test_runs_from_a_chatterlang_script(self, fake_judge):
        fake_judge["decide"] = {"f1": (False, "off topic"), "f2": (True, "on it")}
        pipeline = talkpipe.compile(
            f'| judgeSearchResults[chat_source="{FAKE_CHAT_SOURCE}", chat_model="m", '
            'set_as="judged"]'
        )
        [item] = list(pipeline([{"results": HITS[:2], "question": "q"}]))
        assert [(j["result"].doc_id, j["keep"]) for j in item["judged"]] == [
            ("a", False),
            ("b", True),
        ]

    def test_after_a_vault_search_in_the_pipe_api(
        self, tmp_path, monkeypatch, fake_embedder, fake_judge
    ):
        """The shape the agent runs: vaultSearch piped into the judge."""
        monkeypatch.setenv("TALKPIPE_VAULT_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("TALKPIPE_VAULT_ROOT", raising=False)
        root = tmp_path / "notes"
        root.mkdir()
        (root / "poetry.md").write_text("Poems used as adversarial prompts.")
        (root / "garden.md").write_text("What grows in the garden in spring.")
        folder = folder_index.open_folder(
            root, embedding_source=FAKE_EMBEDDING_SOURCE, embedding_model="fake"
        )
        folder_index.index_folder_now(folder, vectors=True)

        fake_judge["decide"] = {"f1": (True, "the poems"), "f2": (False, "garden")}
        search = VaultSearch(
            vault_path=str(root),
            embedding_source=FAKE_EMBEDDING_SOURCE,
            embedding_model="fake",
            field="query",
            set_as="results",
        )
        judged = (search | _segment(drop_ignored=True)).as_function(
            single_in=True, single_out=True
        )
        item = judged({"query": "adversarial poems", "question": "poems as attacks?"})
        assert len(item["results"]) == 2
        assert [r.document["title"] for r in item["judged"]] == ["poetry.md"]
        assert "Question: poems as attacks?" in fake_judge["prompts"][0]
