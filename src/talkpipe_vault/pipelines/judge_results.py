"""An LLM judge over search results: the ``judgeSearchResults`` segment.

A search returns what is *similar* to its query; this segment decides what
is *useful* for a question. Given a list of hits and the question they were
found for, one model call judges every hit against the question — and,
optionally, against what the search was meant to find and what is already
known from hits kept earlier — and says for each whether to keep it and why.

It is an ordinary segment, usable after any search in any pipeline::

    vaultSearch[vault_path="~/notes", field="query", set_as="results"]
    | judgeSearchResults[question_field="question", set_as="judged"]

With ``drop_ignored`` it is a plain relevance filter that emits only the
kept hits.

The judging itself is talkpipe-agents' generic ``FindingsJudge`` (the judge
behind its findings loop, and so behind the vault's agent): this segment
turns the hits into findings (:func:`search_findings`), has them judged, and
hands the hits back with the verdicts. What it adds is the vault's model
configuration — the chat model is called through talkpipe's ``LLMPrompt``,
so any prompt source talkpipe registers can judge — and the search-result
shape on both sides.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Annotated, Any

from pydantic import BaseModel
from talkpipe.chatterlang import register_segment
from talkpipe.llm.chat import LLMPrompt
from talkpipe.pipe.core import AbstractSegment
from talkpipe.search.abstract import SearchResult
from talkpipe.util.config import parse_key_value_str
from talkpipe.util.data_manipulation import assign_property, extract_property
from talkpipe_agents import Finding, FindingsJudge
from talkpipe_agents import parse_judgements as parse_decisions
from talkpipe_agents.findings import DEFAULT_MAX_CHARS, JUDGE_SYSTEM_PROMPT

from .config import get_chat_model, get_chat_source

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "JUDGE_SYSTEM_PROMPT",
    "Decision",
    "JudgeSearchResults",
    "Judgements",
    "parse_judgements",
    "search_findings",
]


class Decision(BaseModel):
    id: str
    keep: bool
    reason: str = ""


class Judgements(BaseModel):
    decisions: list[Decision]


def parse_judgements(text: str) -> Judgements:
    """The judge's decisions from its reply, however it wrote them.

    talkpipe-agents' lenient reader (JSON bare, fenced or in prose, a bare
    list, or one "<id> <keep|ignore|true|false> <reason>" line per result),
    returned in this module's ``Judgements`` shape. Raises ValueError when
    no decision can be read.
    """
    return Judgements(
        decisions=[
            Decision(id=d.id, keep=d.keep, reason=d.reason)
            for d in parse_decisions(text)
        ]
    )


def search_findings(
    hits: Iterable[Any], rename_fields: dict[str, str] | None = None
) -> list[Finding]:
    """Search hits as findings: the adapter between the vault's results and
    talkpipe-agents' findings loop.

    A hit is a talkpipe ``SearchResult`` or a dict with ``doc_id``, ``score``
    and ``document``; anything else is skipped. A field in *rename_fields* is
    renamed first, unless the new name is already present. The finding's key
    is the hit's ``doc_id`` (else the document), so a chunk found twice is one
    finding, and the hit itself is kept as ``meta["result"]``, which
    ``Finding.to_search_result()`` gives back for the answer and its citations.
    """
    return [
        Finding.from_search_result(hit, rename_fields)
        for hit in hits
        if isinstance(hit, (SearchResult, dict))
    ]


@register_segment("judgeSearchResults")
class JudgeSearchResults(AbstractSegment):
    """
    Segment that has a language model judge search results against a question.

    Expects dict-like input items holding a list of search results (talkpipe
    SearchResult objects or dicts with doc_id, score and document) and the
    question they were found for; optionally also what the search was meant
    to find, notes on what is already known (a list of strings) and the ids
    of results already decided on, which are skipped rather than judged
    again. One model call judges all of an item's results at once.

    Sets on the item a list of judgements in rank order, one per result not
    skipped: dicts {"result": SearchResult, "keep": bool, "reason": str}.
    With drop_ignored, the list holds only the kept SearchResult objects, so
    the segment is a relevance filter after a search. The model is asked for
    JSON and read leniently (prose decisions, one per line, are accepted
    too). When the model fails or leaves a result undecided, that result is
    kept with a reason that starts "not judged"; nothing raises mid-pipeline.
    """

    def __init__(
        self,
        results_field: Annotated[
            str, "The field holding the search results to judge."
        ] = "results",
        question_field: Annotated[
            str, "The field holding the question the results were found for."
        ] = "question",
        looking_for_field: Annotated[
            str, "The field saying what this search was meant to find (optional)."
        ] = "looking_for",
        known_field: Annotated[
            str, "The field holding notes on what is already known (optional)."
        ] = "known",
        seen_field: Annotated[
            str,
            "The field holding the ids of results already decided on; those "
            "results are skipped (optional).",
        ] = "seen",
        set_as: Annotated[str, "The field to set the judgements as."] = "judged",
        drop_ignored: Annotated[
            bool, "If True, set only the kept results, as SearchResult objects."
        ] = False,
        chat_model: Annotated[
            str | None,
            "Model that judges. If None, uses TalkPipe config or default.",
        ] = None,
        chat_source: Annotated[
            str | None,
            "Source/provider of the judging model. If None, uses TalkPipe config or default.",
        ] = None,
        max_chars: Annotated[
            int, "How much of each result's text the judge is shown."
        ] = DEFAULT_MAX_CHARS,
        rename_fields: Annotated[
            str | None,
            "Document fields to rename during normalization, as 'old:new,old:new'. "
            "A field is only renamed when the new name is absent.",
        ] = None,
    ):
        super().__init__()
        self.results_field = results_field
        self.question_field = question_field
        self.looking_for_field = looking_for_field
        self.known_field = known_field
        self.seen_field = seen_field
        self.set_as = set_as
        self.drop_ignored = drop_ignored
        self.max_chars = max(1, max_chars)
        self.rename_fields = (
            parse_key_value_str(rename_fields, require_value=True)
            if rename_fields
            else {}
        )
        chat_model = chat_model if chat_model is not None else get_chat_model()
        chat_source = chat_source if chat_source is not None else get_chat_source()
        # A plain prompt that asks for JSON, read leniently by the generic
        # judge, rather than talkpipe's guided generation: a provider that
        # ignores the output schema (Ollama's cloud models) would fail every
        # judgement there, while its prose reply still parses.
        self._prompt = LLMPrompt(
            model=chat_model,
            source=chat_source,
            system_prompt=JUDGE_SYSTEM_PROMPT,
            multi_turn=False,
        ).as_function(single_in=True, single_out=True)
        # The judge the agent's findings loop runs too: prompt, model call
        # (one at a time — it holds a lock, since an agent's tool calls in
        # one turn run in threads), lenient reading, "not judged" fallback.
        self.findings_judge = FindingsJudge(
            max_chars=self.max_chars, generate=self._generate
        )

    def _generate(self, prompt: str) -> str:
        reply = self._prompt(prompt)
        # A provider answering with structured output hands back a model.
        return reply.model_dump_json() if isinstance(reply, BaseModel) else str(reply)

    def judge(
        self,
        results: Iterable[Any],
        question: str,
        looking_for: str = "",
        known: Iterable[str] = (),
        seen: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Judge *results* for *question*; the list the segment sets.

        *seen* holds the identities (doc_id, else the document) of results
        already decided on; those are left out. A result the model gave no
        decision for, or every result when the model failed, is kept with a
        reason that starts "not judged".
        """
        judgements = self.findings_judge.judge(
            search_findings(results or [], self.rename_fields),
            question,
            looking_for,
            known,
            seen,
        )
        return [
            {"result": j.finding.to_search_result(), "keep": j.keep, "reason": j.reason}
            for j in judgements
        ]

    def transform(self, input_iter: Iterable[Any]) -> Iterator[Any]:
        for item in input_iter:
            results = extract_property(item, self.results_field, fail_on_missing=False)
            question = extract_property(
                item, self.question_field, fail_on_missing=False
            )
            looking_for = extract_property(
                item, self.looking_for_field, fail_on_missing=False
            )
            known = extract_property(item, self.known_field, fail_on_missing=False)
            seen = extract_property(item, self.seen_field, fail_on_missing=False)
            judged = self.judge(
                results or [],
                str(question or ""),
                str(looking_for or ""),
                known or [],
                seen or [],
            )
            if self.drop_ignored:
                assign_property(
                    item, self.set_as, [j["result"] for j in judged if j["keep"]]
                )
            else:
                assign_property(item, self.set_as, judged)
            yield item
