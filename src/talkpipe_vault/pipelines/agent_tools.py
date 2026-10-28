"""The vault's agent tool pack: retrieval tools an ``llmAgent`` can call.

Seven tools let an agent search a vault and gather, chunk by chunk, what a
question should be answered from:

- ``vector_search`` / ``keyword_search`` — the vault's semantic and Whoosh
  searches, callable any number of times with any query. Each search's
  hits come back **already judged** against the question (a batched LLM
  call inside the tool, costing the agent no turn): the relevant ones are
  kept, the rest ignored, and the agent is told what each kept chunk
  contributes. The agent says in ``looking_for`` what the search should
  find that the answer still lacks, so a partial answer counts;
- ``extract_keywords`` — asks an LLM to distill a question into a Whoosh
  query, so the agent need not write Whoosh syntax itself;
- ``read_document`` — loads the whole document a chunk came from (in parts,
  when it is long), on the hunch that more of it is useful; the part read is
  judged like a search hit;
- ``enough_information`` — how a run ends: the agent's own judgement that
  what it kept would answer the question well;
- ``keep_chunk`` / ``ignore_chunk`` — the decisions, by hand. Not in the
  default pack: a search's hits are judged inside the tool, and an agent
  that could skip deciding did. They remain for pipelines that run the
  session without a judge (see below), where the agent decides.

All seven need per-query state (which chunks have been seen, what was kept),
so they are **factories** returning tools bound to one
:class:`AgentSearchSession` — the pattern talkpipe-agents documents for tool
packs that need runtime state. ``TOOL_FACTORIES`` maps each name to its
factory the way talkpipe-agents' registry maps names to tool objects, and
:func:`build_tools` resolves a list of names against it, falling through to
talkpipe-agents' own registry for anything it does not recognize. So a
pipeline picks its tools by name:

    AgentVaultSearch(vault_path=..., tools="vector_search,enough_information,think")

and a name that is neither a vault tool nor a registered generic one fails
at construction, which for a ChatterLang script means at compile time.

The session is handed the judged searches as callables, the way it is
handed the plain ones; without a judged callable a search falls back to the
plain one and its hits are shown undecided for the agent to keep or ignore,
so the module works with no model at all.

Nothing here knows about agents, models, or prompts — the segments in
``agent_search.py`` own that. This module owns only what the tools do and
the state they share, which is what would move to a generic package if
these tools were ever pulled out of the vault.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from strands import tool as strands_tool
from strands.types.tools import ToolContext
from talkpipe.search.abstract import SearchResult
from talkpipe_agents import resolve_agent_tools

from .search_results import normalize_search_result, search_result_key

logger = logging.getLogger(__name__)

# How much of a chunk the agent is shown when a search returns it. Enough
# for a whole chunk at the default indexing settings (three 300-character
# pieces per shingle): the agent decides on what it can see, so a shorter
# preview hides the part of a chunk that would have made it worth keeping.
CHUNK_PREVIEW_CHARS = 1000
# How much of a document one read_document call returns. Long documents are
# read in parts of this size, so a single read cannot crowd the rest of the
# run out of a small model's context window.
DOCUMENT_PART_CHARS = 6000

SearchFn = Callable[[str], Iterable[Any]]
# A judged search takes a request dict -- query, question, looking_for,
# known (notes on the kept chunks), seen (identities already decided) --
# and returns judgements: dicts with result (a hit), keep and reason, as the
# judgeSearchResults segment sets them. The judge alone takes the same
# request with results in place of query.
JudgedSearchFn = Callable[[dict[str, Any]], Iterable[Any]]
# A factory takes the session the tool will share and returns a Strands tool.
ToolFactory = Callable[["AgentSearchSession"], Any]

# Bound-tool factories, by the name a pipeline (or ChatterLang script) uses.
# Populated by @vault_agent_tool below.
TOOL_FACTORIES: dict[str, ToolFactory] = {}

# The pack the interfaces use. keep_chunk and ignore_chunk are registered
# but left out: searches judge their own hits, and an agent given the
# tools to decide by hand spends turns on it or skips it.
DEFAULT_TOOL_NAMES = (
    "vector_search",
    "keyword_search",
    "extract_keywords",
    "read_document",
    "enough_information",
)


def vault_agent_tool(name: str) -> Callable[[ToolFactory], ToolFactory]:
    """Register a session-bound tool factory under *name*."""

    def register(factory: ToolFactory) -> ToolFactory:
        TOOL_FACTORIES[name] = factory
        return factory

    return register


def vault_agent_tool_names() -> list[str]:
    """Every registered vault tool name, in registration order."""
    return list(TOOL_FACTORIES)


def default_tool_names() -> list[str]:
    """The default pack: the vault tools an agent gets when none are named."""
    return list(DEFAULT_TOOL_NAMES)


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

    None (or an empty spec) means the default pack, so a caller that does
    not care about tool selection gets the working default.
    """
    if spec is None:
        return default_tool_names()
    names = spec.split(",") if isinstance(spec, str) else list(spec)
    cleaned = [str(name).strip() for name in names if str(name).strip()]
    return cleaned or default_tool_names()


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
    ``judged_vector_search``/``judged_keyword_search`` take a request dict
    (``query``, ``question``, ``looking_for``, ``known``, ``seen``) and return
    judgements (dicts with ``result``, ``keep``, ``reason``); ``judge`` takes
    the same request with ``results`` in place of ``query``, for the parts
    ``read_document`` reads. Without them, ``vector_search``/``keyword_search``
    take a query and return search hits (SearchResult objects or dicts with
    doc_id/score/document), which are shown to the agent undecided for it to
    keep or ignore by hand;
    ``keyword_index_exists`` says whether keyword search can run at all;
    ``extract_keywords`` asks an LLM to turn a question into a keyword query
    (None when no model is available); ``read_document`` takes the source a
    hit's document records (its ``source`` or ``path``) and returns that
    document's whole text (None when documents cannot be read).
    ``question`` is what the judge judges against. ``max_turns`` is the
    agent's turn cap, if one is set: every tool reply then says how many
    turns are left, counted from what the agent executing the tool reports
    (see :func:`note_turn`), and the last one is announced.
    """

    def __init__(
        self,
        *,
        vector_search: SearchFn | None = None,
        keyword_search: SearchFn | None = None,
        keyword_index_exists: Callable[[], bool],
        extract_keywords: Callable[[str], str] | None,
        max_chunks: int,
        tell: Callable[[str], None],
        rename_fields: dict[str, str] | None = None,
        read_document: Callable[[str], str] | None = None,
        question: str = "",
        judged_vector_search: JudgedSearchFn | None = None,
        judged_keyword_search: JudgedSearchFn | None = None,
        judge: JudgedSearchFn | None = None,
        max_turns: int | None = None,
    ) -> None:
        self._vector_search = vector_search
        self._keyword_search = keyword_search
        self._judged_vector_search = judged_vector_search
        self._judged_keyword_search = judged_keyword_search
        self._judge = judge
        self._keyword_index_exists = keyword_index_exists
        self._extract_keywords = extract_keywords
        self._read_document = read_document
        self.question = question.strip()
        self.max_chunks = max(1, max_chunks)
        self.max_turns = max(1, max_turns) if max_turns is not None else None
        # Model turns the run has used so far, as reported by the tools.
        self.turns_used = 0
        self._tell = tell
        self._rename_fields = rename_fields or {}
        self._chunks: dict[str, _Chunk] = {}
        self._by_key: dict[str, str] = {}
        self.kept: list[str] = []
        # What each kept chunk contributes, by chunk id: the judge's reason
        # (or the agent's), shown to the judge as "already known".
        self.kept_notes: dict[str, str] = {}
        self.ignored: set[str] = set()
        self.trace: list[str] = []
        self.queries_run: list[tuple[str, str]] = []
        # Set by the enough_information tool: the agent's own judgement that
        # what it has kept would answer the question well. This, not the
        # number of chunks, is the run's stopping criterion.
        self.enough = False
        self.enough_reason = ""
        # Strands runs the tool calls of one model turn concurrently, in
        # threads, so two searches can register hits at the same moment;
        # the lock keeps them from handing out the same chunk id.
        self._register_lock = threading.Lock()

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

    @property
    def undecided(self) -> list[str]:
        """Ids of the chunks the agent was shown but neither kept nor ignored."""
        return [
            cid
            for cid in self._chunks
            if cid not in self.kept and cid not in self.ignored
        ]

    def known_notes(self) -> list[str]:
        """What is already known, one line per kept chunk: its title and
        what it contributes. The judge sees these so a hit that repeats
        them is ignored and one that adds to them is kept."""
        notes = []
        for cid in self.kept:
            chunk = self._chunks[cid]
            parts = [self._title(chunk), self.kept_notes.get(cid, "")]
            notes.append(" — ".join(p for p in parts if p) or cid)
        return notes

    def decided_keys(self) -> list[str]:
        """The identities of every chunk already kept or ignored, so a judged
        search skips them instead of judging them again."""
        return [
            chunk.key
            for cid, chunk in self._chunks.items()
            if cid in self.kept or cid in self.ignored
        ]

    def _judge_request(self, looking_for: str, **extra: Any) -> dict[str, Any]:
        return {
            "question": self.question,
            "looking_for": looking_for.strip(),
            "known": self.known_notes(),
            "seen": self.decided_keys(),
            **extra,
        }

    def _register(self, entry: Any, key: str | None = None) -> _Chunk | None:
        result = normalize_search_result(entry, self._rename_fields)
        if result is None:
            return None
        key = key or search_result_key(result)
        with self._register_lock:
            existing = self._by_key.get(key)
            if existing is not None:
                return self._chunks[existing]
            chunk = _Chunk(id=f"c{len(self._chunks) + 1}", result=result, key=key)
            self._chunks[chunk.id] = chunk
            self._by_key[key] = chunk.id
            return chunk

    @staticmethod
    def _title(chunk: _Chunk) -> str:
        doc = chunk.result.document or {}
        return str(
            doc.get("title") or doc.get("filename") or doc.get("source") or ""
        ).strip()

    def _describe(self, chunk: _Chunk, *, status: bool = True) -> str:
        doc = chunk.result.document or {}
        title = self._title(chunk)
        text = str(doc.get("content") or doc.get("shingle") or "").strip()
        text = " ".join(text.split())
        if len(text) > CHUNK_PREVIEW_CHARS:
            text = text[:CHUNK_PREVIEW_CHARS] + "…"
        head = f"[{chunk.id}]"
        if status and chunk.id in self.kept:
            head += " (already kept)"
        elif status and chunk.id in self.ignored:
            head += " (already ignored)"
        if title:
            head += f" {title}"
        return f"{head}\n{text}"

    def note_turn(self, turns_used: int) -> None:
        """Record that the run has used *turns_used* model turns.

        The tools report it from the agent executing them (Strands counts a
        model call as a cycle, and the turn cap is a cap on cycles), so the
        status line can say how many turns are left. Never goes backwards.
        """
        self.turns_used = max(self.turns_used, turns_used)

    @property
    def turns_left(self) -> int | None:
        """Turns the agent can still take after this reply, or None when no
        cap is known. 1 means the next turn is its last."""
        if self.max_turns is None:
            return None
        return max(0, self.max_turns - self.turns_used)

    def turns_note(self) -> str:
        """How many turns are left, for the model; "" when no cap is known."""
        left = self.turns_left
        if left is None:
            return ""
        if left == 0:
            return "No turns left."
        if left == 1:
            # The reply the model is reading is the last one it can act on:
            # a search now would end the run before it saw the results.
            return (
                "1 turn left: the next one is your last, so call "
                "enough_information now rather than searching again."
            )
        return f"{left} of {self.max_turns} turns left."

    def _status_line(self) -> str:
        status = f"Kept {len(self.kept)} chunk(s), at most {self.max_chunks}."
        return with_turns(self, status)

    def _apply(self, chunk: _Chunk, reason: str, *, keep: bool) -> None:
        """Record a decision on *chunk* and trace it."""
        reason = reason.strip().rstrip(".")
        if keep:
            self.ignored.discard(chunk.id)
            self.kept.append(chunk.id)
            self.kept_notes[chunk.id] = reason
        else:
            self.ignored.add(chunk.id)
        verb = "keep" if keep else "ignore"
        why = f" — {reason}" if reason else ""
        # Name the document too: the trace is read by a person, who never
        # saw which chunk the short id stands for.
        title = self._title(chunk)
        label = f" ({title})" if title else ""
        self.tell(f"agent · {verb} {chunk.id}{label}{why}. {self._status_line()}")

    # -- tool bodies ---------------------------------------------------------

    def run_search(
        self,
        kind: str,
        query: str,
        search: SearchFn | None,
        judged: JudgedSearchFn | None = None,
        looking_for: str = "",
    ) -> str:
        query = query.strip()
        if not query:
            return "The query is empty."
        if (kind, query) in self.queries_run:
            return f"You already ran this {kind} query; try a different one."
        if judged is not None:
            self.queries_run.append((kind, query))
            try:
                request = self._judge_request(looking_for, query=query)
                judgements = list(judged(request) or [])
            except Exception as exc:
                return self._search_failed(kind, exc)
            return self._apply_judgements(kind, query, judgements)
        if search is None:
            return f"The {kind} search is unavailable here."
        self.queries_run.append((kind, query))
        try:
            hits = list(search(query) or [])
        except Exception as exc:
            return self._search_failed(kind, exc)
        return self._show_undecided(kind, query, hits)

    def _search_failed(self, kind: str, exc: Exception) -> str:
        logger.warning("Agent %s search failed: %s", kind, exc, exc_info=exc)
        self.tell(f"agent ← {kind}_search failed: {exc}")
        return f"The {kind} search failed: {exc}"

    def _show_undecided(self, kind: str, query: str, hits: list[Any]) -> str:
        """Report a plain search's hits for the agent to decide on."""
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

    def _apply_judgements(self, kind: str, query: str, judgements: list[Any]) -> str:
        """Apply a judged search's decisions and report them to the agent.

        Decisions are applied in rank order; a chunk judged worth keeping
        once the chunk maximum is reached is not kept (nor registered, so it
        is not left undecided), and the agent is told how many were passed
        over. Hits already decided on are skipped either way.
        """
        kept: list[_Chunk] = []
        ignored: list[tuple[_Chunk, str]] = []
        passed_over = 0
        seen: set[str] = set()
        for entry in judgements:
            if not isinstance(entry, dict):
                continue
            keep = bool(entry.get("keep"))
            reason = str(entry.get("reason") or "")
            if keep and self.at_capacity:
                passed_over += 1
                continue
            chunk = self._register(entry.get("result"))
            if chunk is None or chunk.id in seen:
                continue
            seen.add(chunk.id)
            if chunk.id in self.kept or chunk.id in self.ignored:
                continue
            self._apply(chunk, reason, keep=keep)
            if keep:
                kept.append(chunk)
            else:
                ignored.append((chunk, reason))
        total = len(kept) + len(ignored) + passed_over
        self.tell(
            f"agent ← {kind}_search: {total} hit(s), kept {len(kept)}, "
            f"ignored {len(ignored)}"
            + (f", {passed_over} passed over" if passed_over else "")
            + f". {self._status_line()}"
        )
        if not total:
            return f"No new {kind} results for {query!r}. Try different terms."
        summary = f"kept {len(kept)}, ignored {len(ignored)}"
        lines = [f"{total} result(s) for {query!r}: {summary}."]
        if kept:
            lines.append("Kept:")
            lines.extend(self._describe(c, status=False) for c in kept)
        if ignored:
            lines.append("Ignored:")
            for chunk, reason in ignored:
                title = self._title(chunk)
                line = f"[{chunk.id}]" + (f" {title}" if title else "")
                lines.append(line + (f" — {reason}" if reason else ""))
        if passed_over:
            lines.append(
                f"{passed_over} more relevant result(s) were not kept: the "
                f"{self.max_chunks}-chunk maximum is reached."
            )
        lines.append(self._known_summary())
        if self.at_capacity:
            lines.append(
                f"You have kept the maximum of {self.max_chunks} chunks for "
                "this run. Stop calling tools and reply: done"
            )
        else:
            lines.append(
                f"{self._status_line()} Search for what the answer still "
                "lacks, or call enough_information once what is known would "
                "answer the question well."
            )
        return "\n\n".join(lines)

    def _known_summary(self) -> str:
        notes = self.known_notes()
        if not notes:
            return "Already known: nothing yet."
        return "Already known:\n" + "\n".join(f"- {note}" for note in notes)

    def vector_search(self, query: str, looking_for: str = "") -> str:
        return self.run_search(
            "vector",
            query,
            self._vector_search,
            self._judged_vector_search,
            looking_for,
        )

    def keyword_search(self, query: str, looking_for: str = "") -> str:
        if not self._keyword_index_exists():
            self.tell("agent ← keyword_search: no keyword index exists")
            return (
                "This vault has no full-text index, so keyword search is "
                "unavailable; use vector_search instead."
            )
        return self.run_search(
            "keyword",
            query,
            self._keyword_search,
            self._judged_keyword_search,
            looking_for,
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

    def _unknown_chunk(self, tool_name: str, chunk_id: str) -> str:
        # Traced as well as returned: a model that guesses ids, or keeps a
        # chunk in the same turn as the search that finds it, otherwise
        # leaves a trace full of keep calls and nothing kept.
        self.tell(f"agent ✗ {tool_name} refused: unknown chunk id {chunk_id!r}")
        return (
            f"Unknown chunk id {chunk_id!r}; use an id from a search result "
            "you have already received (a search called in the same step "
            "has not returned its ids yet)."
        )

    def read_document(
        self, chunk_id: str, start: int | None = None, looking_for: str = ""
    ) -> str:
        """Read the document *chunk_id* came from, one part at a time.

        The part read is registered as a chunk of its own and, when the
        session has a judge, judged like a search hit — otherwise the agent
        keeps or ignores it — so a kept part reaches the answer (and its
        citations) like any other chunk. Only documents the vault itself
        returned can be read: the agent names a chunk, never a path.
        """
        chunk_id = chunk_id.strip()
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            return self._unknown_chunk("read_document", chunk_id)
        doc = chunk.result.document or {}
        source = str(doc.get("source") or doc.get("path") or "").strip()
        if not source:
            return f"{chunk_id} records no source document to read."
        if self._read_document is None:
            return "Reading whole documents is unavailable here."
        title = self._title(chunk)
        try:
            text = self._read_document(source).strip()
        except Exception as exc:
            logger.warning("Agent document read failed", exc_info=True)
            self.tell(f"agent ← read_document {chunk_id} ({title}) failed: {exc}")
            return f"The document could not be read: {exc}"
        start = max(0, start or 0)
        if start >= len(text):
            self.tell(f"agent ← read_document {chunk_id} ({title}): nothing at {start}")
            return (
                f"The document has {len(text)} characters; there is nothing "
                f"from character {start} on."
            )
        part = text[start : start + DOCUMENT_PART_CHARS]
        end = start + len(part)
        fields = {
            k: v
            for k, v in doc.items()
            if k not in {"content", "shingle", "shingle_text", "_doc_id", "doc_id"}
        }
        # The source as doc_id lets the interfaces open the cited document.
        entry = SearchResult(
            score=chunk.result.score,
            doc_id=source,
            document={**fields, "content": part},
        )
        new = self._register(entry, key=f"document:{source}:{start}")
        if new is None:  # pragma: no cover - a SearchResult always normalizes
            return "The document could not be read."
        span = f"characters {start}-{end} of {len(text)}"
        self.tell(f"agent ← read_document {chunk_id} ({title}): {span} as {new.id}")
        more = (
            f" Call read_document with start={end} to read on."
            if end < len(text)
            else ""
        )
        verdict = self._judge_part(new, looking_for)
        if verdict is None:
            return (
                f"[{new.id}] {title} — the document, {span}. Keep or ignore "
                f"{new.id} like any chunk.{more}\n\n{part}"
            )
        return f"[{new.id}] {title} — the document, {span}. {verdict}{more}\n\n{part}"

    def _judge_part(self, chunk: _Chunk, looking_for: str) -> str | None:
        """Judge a document part the way a search hit is judged; None when
        the session has no judge (or the part was decided on before)."""
        if self._judge is None:
            return None
        if chunk.id in self.kept:
            return f"Already kept{self._note_suffix(chunk)}."
        if chunk.id in self.ignored:
            return "Already ignored."
        try:
            judgements = list(
                self._judge(self._judge_request(looking_for, results=[chunk.result]))
                or []
            )
        except Exception as exc:
            logger.warning("Judging a document part failed", exc_info=True)
            self.tell(f"agent ← judging {chunk.id} failed: {exc}")
            return "It could not be judged; keep or ignore it with keep_chunk or ignore_chunk."
        verdict = next((j for j in judgements if isinstance(j, dict)), None)
        if verdict is None:
            return "It could not be judged; keep or ignore it with keep_chunk or ignore_chunk."
        keep = bool(verdict.get("keep"))
        reason = str(verdict.get("reason") or "")
        if keep and self.at_capacity:
            return (
                f"It would help, but the {self.max_chunks}-chunk maximum is "
                "reached, so it was not kept. Stop calling tools and reply: done"
            )
        self._apply(chunk, reason, keep=keep)
        why = f": {reason}" if reason else ""
        return f"{'Kept' if keep else 'Ignored'}{why}. {self._status_line()}"

    def _note_suffix(self, chunk: _Chunk) -> str:
        note = self.kept_notes.get(chunk.id, "")
        return f" — {note}" if note else ""

    def decide(self, chunk_id: str, reason: str, *, keep: bool) -> str:
        chunk_id = chunk_id.strip()
        verb = "keep" if keep else "ignore"
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            return self._unknown_chunk(f"{verb}_chunk", chunk_id)
        if chunk_id in self.kept:
            return f"{chunk_id} is already kept."
        if chunk_id in self.ignored and not keep:
            return f"{chunk_id} is already ignored."
        self._apply(chunk, reason, keep=keep)
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
                "from. Keep searching, with different terms, for what would "
                "answer the question, even if only in part."
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


def with_turns(session: AgentSearchSession, reply: str) -> str:
    """*reply* with the session's turn count appended, once.

    Every tool reply ends with how many turns are left, whether or not it
    carried the status line that already says so.
    """
    note = session.turns_note()
    if note and note not in reply:
        return f"{reply} {note}"
    return reply


def note_turn(session: AgentSearchSession, tool_context: Any) -> None:
    """Tell *session* how many turns the agent executing a tool has used.

    Strands hands a tool declared with ``context=True`` a ``ToolContext``
    whose ``agent`` carries the event-loop metrics; the cycles of the
    current invocation are the model calls so far, which is what the turn
    cap counts. Called outside an agent (tests, a script) there is no
    context, and this does nothing.
    """
    agent = getattr(tool_context, "agent", None)
    metrics = getattr(agent, "event_loop_metrics", None)
    invocation = getattr(metrics, "latest_agent_invocation", None)
    cycles = getattr(invocation, "cycles", None)
    if cycles is not None:
        session.note_turn(len(cycles))


@vault_agent_tool("vector_search")
def make_vector_search(session: AgentSearchSession) -> Any:
    @strands_tool(context=True)
    def vector_search(
        query: str, looking_for: str = "", tool_context: ToolContext | None = None
    ) -> str:
        """Semantic search over the vault: finds chunks whose meaning is
        close to the query, even when the words differ. Best with a
        full question or descriptive phrase. The results come back
        already judged against the question: the relevant ones are kept
        for the answer, the rest ignored, and you are told what each kept
        chunk contributes and what is already known.

        Args:
            query: A natural-language question or descriptive phrase.
            looking_for: What this search should find that the answer
                still lacks, in one phrase, so a result supplying just
                that is kept. Leave it empty for the question itself.
        """
        note_turn(session, tool_context)
        return with_turns(
            session,
            guard(session, "vector_search")
            or session.vector_search(query, looking_for),
        )

    return vector_search


@vault_agent_tool("keyword_search")
def make_keyword_search(session: AgentSearchSession) -> Any:
    @strands_tool(context=True)
    def keyword_search(
        query: str, looking_for: str = "", tool_context: ToolContext | None = None
    ) -> str:
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
        note_turn(session, tool_context)
        return with_turns(
            session,
            guard(session, "keyword_search")
            or session.keyword_search(query, looking_for),
        )

    return keyword_search


@vault_agent_tool("extract_keywords")
def make_extract_keywords(session: AgentSearchSession) -> Any:
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
        note_turn(session, tool_context)
        return with_turns(
            session,
            guard(session, "extract_keywords") or session.extract_keywords(question),
        )

    return extract_keywords


@vault_agent_tool("read_document")
def make_read_document(session: AgentSearchSession) -> Any:
    @strands_tool(context=True)
    def read_document(
        chunk_id: str,
        start: int | None = None,
        looking_for: str = "",
        tool_context: ToolContext | None = None,
    ) -> str:
        """Read the whole document a chunk came from, when you suspect more
        of it would help: the chunk is cut off, it is part of a list or a
        procedure, or its document looks like the main place the question
        is covered. The part read becomes a new chunk with its own id,
        judged against the question like a search result. Long documents
        come in parts.

        Args:
            chunk_id: The id of a chunk from that document, e.g. c3.
            start: Character offset to read from; leave it out to read from
                the beginning, or pass the offset a previous read gave.
            looking_for: What you hope this part of the document holds, in
                one phrase, so it is kept when it supplies just that.
        """
        note_turn(session, tool_context)
        return with_turns(
            session,
            guard(session, "read_document")
            or session.read_document(chunk_id, start, looking_for),
        )

    return read_document


@vault_agent_tool("keep_chunk")
def make_keep_chunk(session: AgentSearchSession) -> Any:
    @strands_tool(context=True)
    def keep_chunk(
        chunk_id: str, reason: str = "", tool_context: ToolContext | None = None
    ) -> str:
        """Keep a chunk: it holds information that helps answer the
        question, even if only part of it. Kept chunks are the only thing
        the question will be answered from.

        Args:
            chunk_id: The id shown with the chunk, e.g. c3.
            reason: One short phrase saying what it contributes.
        """
        note_turn(session, tool_context)
        return with_turns(
            session,
            guard(session, "keep_chunk") or session.decide(chunk_id, reason, keep=True),
        )

    return keep_chunk


@vault_agent_tool("ignore_chunk")
def make_ignore_chunk(session: AgentSearchSession) -> Any:
    @strands_tool(context=True)
    def ignore_chunk(
        chunk_id: str, reason: str = "", tool_context: ToolContext | None = None
    ) -> str:
        """Ignore a chunk: it is about something other than the question,
        so nothing in it would help the answer.

        Args:
            chunk_id: The id shown with the chunk, e.g. c3.
            reason: One short phrase saying why it is irrelevant.
        """
        note_turn(session, tool_context)
        return with_turns(
            session,
            guard(session, "ignore_chunk")
            or session.decide(chunk_id, reason, keep=False),
        )

    return ignore_chunk


@vault_agent_tool("enough_information")
def make_enough_information(session: AgentSearchSession) -> Any:
    @strands_tool(context=True)
    def enough_information(
        reason: str = "", tool_context: ToolContext | None = None
    ) -> str:
        """Declare that the chunks kept so far would let you answer the
        question well, and end the search. This is how a run should end:
        call it as soon as that is true, however many chunks that took.
        Call it also when repeated searches find nothing new and the
        question still cannot be fully answered, saying what is missing.

        Args:
            reason: One short phrase saying what the kept chunks cover,
                or what is still missing.
        """
        note_turn(session, tool_context)
        return with_turns(
            session,
            guard(session, "enough_information") or session.declare_enough(reason),
        )

    return enough_information
