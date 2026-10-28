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
kept hits. The agent in ``agent_search`` runs its searches through it, so
every hit the agent is shown already carries a decision.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Iterator
from typing import Annotated, Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from talkpipe.chatterlang import register_segment
from talkpipe.llm.chat import LLMPrompt
from talkpipe.pipe.core import AbstractSegment
from talkpipe.search.abstract import SearchResult
from talkpipe.util.config import parse_key_value_str
from talkpipe.util.data_manipulation import assign_property, extract_property

from .config import get_chat_model, get_chat_source
from .search_results import normalize_search_result, search_result_key

logger = logging.getLogger(__name__)

# How much of a hit the judge is shown. Enough for a whole chunk at the
# default indexing settings (three 300-character pieces per shingle): the
# judge decides on what it can see, so a shorter preview hides the part of a
# chunk that would have made it worth keeping.
DEFAULT_MAX_CHARS = 1000

JUDGE_SYSTEM_PROMPT = """You judge search results for a system that answers questions from a personal document collection. You are shown a question; optionally what the search that found these results was meant to find, and what is already known from results kept earlier; then the results, each labelled r1, r2, and so on.

Decide for every result whether to keep it or ignore it. Keep a result if it helps answer the question, even in part, or if it supplies what the search was looking for even when it does not address the whole question. In either case it must add something that is not already known: a new fact, a missing piece of the answer, or corroboration from a different document. Ignore a result that is about something else or only shares words with the question, and ignore a near-duplicate of what is already known. Read the question's terms in their ordinary sense: a word names what it usually names, not some other thing that happens to share the name. When unsure, keep.

Reply with one decision per result, in order, giving the result's id, keep (true or false) and a short reason: for a kept result, what it contributes to the answer; for an ignored one, why it does not help. Reply with JSON only, in this shape and nothing else:
{"decisions": [{"id": "r1", "keep": true, "reason": "..."}, {"id": "r2", "keep": false, "reason": "..."}]}"""


class Decision(BaseModel):
    id: str
    keep: bool
    reason: str = ""


class Judgements(BaseModel):
    decisions: list[Decision]


_DECISIONS = TypeAdapter(list[Decision])
# A decision written as a line: "r1: true - reason", "[r2] ignore: reason",
# "3 keep reason". For models that answer the prompt in prose rather than
# the JSON it asks for (Ollama's cloud models ignore its format schema).
_LINE_RE = re.compile(
    r"^\W*(r?\d+)\b\W*(keep|ignore|true|false|yes|no)\b\W*(.*)$", re.IGNORECASE
)
_KEEP_WORDS = frozenset({"keep", "true", "yes"})


def parse_judgements(text: str) -> Judgements:
    """The judge's decisions from its reply, however it wrote them.

    The prompt asks for one JSON object; that is accepted bare, fenced, or
    surrounded by prose, as is a bare JSON list of decisions. Failing that,
    every line that reads as "<id> <keep|ignore|true|false> <reason>" is a
    decision. Raises ValueError when no decision can be read.
    """
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return Judgements.model_validate_json(text[start : end + 1])
        except ValidationError:
            pass
    if text.startswith("[") and text.endswith("]"):
        try:
            return Judgements(decisions=_DECISIONS.validate_json(text))
        except ValidationError:
            pass
    decisions = []
    for line in text.splitlines():
        match = _LINE_RE.match(line)
        if match is None:
            continue
        label, verdict, reason = match.groups()
        decisions.append(
            Decision(
                id=label,
                keep=verdict.lower() in _KEEP_WORDS,
                # Hyphens and the en and em dashes models separate with.
                reason=reason.strip(" \t:-–—.,"),  # noqa: RUF001 - the dashes are the point
            )
        )
    if not decisions:
        raise ValueError("the judge's reply held no decisions")
    return Judgements(decisions=decisions)


def _one_line(exc: Exception, limit: int = 160) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label(result: SearchResult) -> str:
    doc = result.document or {}
    return str(doc.get("title") or doc.get("filename") or doc.get("source") or "")


def _text(result: SearchResult, max_chars: int) -> str:
    doc = result.document or {}
    text = " ".join(str(doc.get("content") or doc.get("shingle") or "").split())
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


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
        # A plain prompt that asks for JSON, parsed by parse_judgements,
        # rather than talkpipe's guided generation: a provider that ignores
        # the output schema (Ollama's cloud models) would fail every
        # judgement there, while its prose reply still parses here.
        self._generate = LLMPrompt(
            model=chat_model,
            source=chat_source,
            system_prompt=JUDGE_SYSTEM_PROMPT,
            multi_turn=False,
        ).as_function(single_in=True, single_out=True)
        # A prompt adapter keeps the message it is sending until the reply
        # comes back, so two judgements at once (an agent's searches in one
        # turn run in threads) would be sent as one request; take turns.
        self._lock = threading.Lock()

    @staticmethod
    def prompt(
        question: str,
        looking_for: str,
        known: Iterable[str],
        labelled: list[tuple[str, SearchResult]],
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> str:
        """The prompt for one judgement: the question, what the search was
        looking for, what is already known, and the labelled results."""
        lines = [f"Question: {question.strip()}"]
        if looking_for.strip():
            lines.append(f"This search is looking for: {looking_for.strip()}")
        notes = [str(note).strip() for note in known if str(note).strip()]
        lines.append("Already known:")
        lines.extend(f"- {note}" for note in notes)
        if not notes:
            lines.append("- nothing yet")
        lines.append("Results:")
        for label, result in labelled:
            head = f"[{label}]"
            if title := _label(result):
                head += f" {title}"
            lines.append(f"{head}\n{_text(result, max_chars)}")
        return "\n".join(lines)

    @staticmethod
    def _label_of(decision_id: str) -> str:
        # Models write the label as "r1", "R1", "[r1]" or just "1"; all
        # mean the same result.
        label = "".join(ch for ch in decision_id.lower() if ch.isalnum())
        return f"r{label}" if label.isdigit() else label

    def _decisions(self, prompt: str) -> dict[str, Decision]:
        with self._lock:
            raw = self._generate(prompt)
        if not isinstance(raw, Judgements):
            raw = parse_judgements(str(raw))
        return {self._label_of(d.id): d for d in raw.decisions}

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
        skip = {str(key) for key in seen}
        labelled: list[tuple[str, SearchResult]] = []
        for entry in results or []:
            result = normalize_search_result(entry, self.rename_fields)
            if result is None or search_result_key(result) in skip:
                continue
            labelled.append((f"r{len(labelled) + 1}", result))
        if not labelled:
            return []
        prompt = self.prompt(question, looking_for, known, labelled, self.max_chars)
        try:
            decisions = self._decisions(prompt)
        except Exception as exc:
            logger.warning("Judging search results failed; keeping them", exc_info=True)
            decisions = {}
            undecided = f"not judged: {_one_line(exc)}"
        else:
            undecided = "not judged: the model gave no decision for it"
        judged = []
        for label, result in labelled:
            decision = decisions.get(label)
            if decision is None:
                judged.append({"result": result, "keep": True, "reason": undecided})
            else:
                judged.append(
                    {
                        "result": result,
                        "keep": decision.keep,
                        "reason": decision.reason.strip(),
                    }
                )
        return judged

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
