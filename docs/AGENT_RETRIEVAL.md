# Agent retrieval — developer's guide

How the vault's agent-driven retrieval is built, and how to use its pieces in
your own application: with a different search backend, a different agent, a
different set of tools, or none of the vault at all.

Everything described here lives in three modules, split on purpose:

| Module | Owns | Knows about |
|---|---|---|
| `talkpipe_vault/pipelines/judge_results.py` | **The judge** — the `judgeSearchResults` segment: one model call that decides, for every hit of a search, whether it helps answer the question | A chat model. Nothing about agents, tools, or vaults |
| `talkpipe_vault/pipelines/agent_tools.py` | **What the tools do** — the seven retrieval tools and the per-query state they share | Search callables you hand it, judged or plain. Nothing about agents, models, prompts, or vaults |
| `talkpipe_vault/pipelines/agent_search.py` | **The pipeline that runs them** — the `vaultAgentSearch` / `vaultAgentChat` segments | The vault on disk, the embedder, the judge, the RAG prompt, talkpipe-agents' `llmAgent` |

If you want the judge after your own search, you take the first module. If
you want the tools in another application, you take the second (and the
first, if you want judged searches). If you want the vault's behaviour with
a different agent, you keep all three and change one parameter. All of
these are shown below.

## The sixty-second version

In Python:

```python
from talkpipe_vault.pipelines.agent_search import AgentVaultChat

ask = AgentVaultChat(
    vault_path="~/notes",  # a folder opened in vault-server, or a named vault
    chat_source="ollama",
    chat_model="qwen3:8b",  # a model that handles tool calls well — see below
).as_function(single_in=True, single_out=True)
outcome = ask("How did the treasury handle inflation in the 1970s?")

outcome.response   # the answer, with a "Sources:" list appended
outcome.results    # the chunks it was built from, as talkpipe SearchResult, in keep order
outcome.note       # one line: how the run ended (see "Reading the outcome")
outcome.trace      # every tool call and verdict, as printed to the console
```

In ChatterLang, as a segment like any other — run it with TalkPipe's
`chatterlang_script --script '<the script>'` (installed with the vault):

```
INPUT FROM echo[data="How did the treasury handle inflation in the 1970s?"]
| vaultAgentChat[vault_path="~/notes-vault", chat_source="ollama", chat_model="qwen3:8b"]
| extractProperty[property="response"]
| print
```

`vaultAgentChat` emits the whole outcome; `extractProperty` keeps just the
answer (drop it to print everything). The trace lines are printed to stdout
as the agent works, ahead of the answer — a ChatterLang script cannot turn
that off, so its output always carries them.

**Keep the trace off stdout** in your own program by passing `progress`, a
callable that receives each trace line: `progress=logging.getLogger("my_app").info`,
a list's `append`, or `lambda line: None` to drop them. `outcome.trace` holds
the lines either way.

**Pick the model.** The chat model defaults to TalkPipe's configuration
(`TALKPIPE_CHAT_SOURCE` / `TALKPIPE_CHAT_MODEL`, or `chat_source` /
`chat_model` in `~/.talkpipe.toml`) and then to the vault's default,
`ollama` / `mistral-small`. The same model drives the agent and judges each
search's hits. A script does **not** see the model chosen on the web
interface's Settings page, so pass `chat_model` (and `chat_source`) or set
the configuration. The other parameters (`max_chunks`, `max_iterations`,
`embedding_model`, …) are listed, with what each does, by
`help(AgentVaultSearch)`.

`vaultAgentSearch` is the same thing without the answering stage: it emits an
`AgentSearchOutcome` (results, note, trace) and is what the Semantic Search
page uses. `vaultAgentChat` subclasses it, and takes exactly the same
parameters plus nothing — the answer is generated from whatever the search
kept.

`vault_path` may also be a list of vaults or folders (each searched with its
own embedder and the hits fused) or a folder with a folder-mode index; see
[Advanced Usage: Folder mode](ADVANCED.md#folder-mode) and the
[Developer Guide](DEVELOPER_GUIDE.md).

## How a run works

The agent is talkpipe-agents' `llmAgent` segment. It is given five tools by
default, all bound to one `AgentSearchSession` that holds that query's
state:

| Tool | What it does |
|---|---|
| `vector_search(query, looking_for)` | Semantic search; the hits come back judged, each with a short id (`c1`, `c2`, …) |
| `keyword_search(query, looking_for)` | Full-text search (Whoosh syntax), judged the same way; says so if the vault has no index |
| `extract_keywords(question)` | Asks an LLM to turn a plain-language question into a keyword query |
| `read_document(chunk_id, start, looking_for)` | Loads the whole document a chunk came from, as a new chunk judged like a hit; long documents come in `DOCUMENT_PART_CHARS` (6000-character) parts, `start` picking which |
| `enough_information(reason)` | **How a run ends:** the agent's judgement that what has been kept would answer the question well |

Two more are registered but not in the default pack — `keep_chunk(chunk_id,
reason)` and `ignore_chunk(chunk_id, reason)`, the decisions by hand. They
are for a session run without a judge (see [Using the tools in another
application](#using-the-tools-in-another-application)); name them in
`tools=` to get them.

**Each search is a TalkPipe pipeline: search → judge.** `vector_search`
runs `vaultSearch | judgeSearchResults`, `keyword_search` runs
`vaultTextSearch | judgeSearchResults`. The judge
([Developer Guide: Judging results](DEVELOPER_GUIDE.md#judging-results-judgesearchresults))
is one batched model call inside the tool, so it costs the agent no turn and
cannot be skipped. It sees the user's question, what the agent said this
search is `looking_for`, and what is already known — one line per kept
chunk, its title and what it contributes — and keeps a hit that helps answer
the question even in part, or that supplies what the search was looking for,
provided it adds something to what is known. It ignores a hit that only
shares words with the question, a near-duplicate, or something that reads a
word in the question as another thing with the same name. When unsure, it
keeps. Hits already decided on are skipped, not judged again; `max_chunks`
is respected in rank order, and a hit judged worth keeping once the maximum
is full is passed over and the agent told.

So the tool's reply is: the kept chunks in full, one line per ignored chunk
with the reason, the current "already known" notes, and the status. The
agent's job is what to search for next — to say in `looking_for` what the
answer still lacks (a fact, a date, the other side of a comparison, a
corroborating document), using terms from the kept chunks, and to stop when
what is known would answer the question. Chunks are deduplicated across
searches by `doc_id` (falling back to the document's content), so a chunk
found twice keeps its id and its verdict. The judge sees up to
`CHUNK_PREVIEW_CHARS` (1000) characters of each hit, enough for a whole
chunk at the default indexing settings; the agent sees the same.

`read_document` is for a hunch that a chunk's document holds more of the
answer — the chunk is cut off, it is one step of a procedure, or its document
is plainly where the topic is covered. The agent names a chunk it has seen,
never a path, so it can only read documents the vault itself returned, and
the vault reads them with the extractors indexing used. The part it reads is
registered as a chunk like a search hit and judged the same way (`looking_for`
saying what the agent hopes it holds); a kept part reaches the answer, and
its citation opens the document, like any other. A document that is no
longer where it was indexed is reported to the agent, not an error.

**The stopping criterion is the agent's judgement, not a count.** A run ends
when the agent calls `enough_information`. Two limits exist only to bound a
run that never gets there: `max_chunks` (the most chunks one run may keep;
Settings → *Agent Chunk Limit*) and `max_iterations` (agent turns; Settings
→ *Agent Search Iterations*; the Search and Ask pages' *Extra Effort* box
doubles it for one question, via `with_max_iterations`, a copy of the
segment that shares its vault and model bindings). Once either the judgement is made or the chunk
ceiling is full, every tool refuses further calls with the same short message
telling the model to stop — the rule is enforced inside the tools, so it
holds behind any agent, not just the one the vault builds.

The agent is told its turn budget. The opening prompt says how many turns
it has, and every tool reply ends with how many are left (`5 of 8 turns
left.`), the last one spelled out (`1 turn left: the next one is your last,
so call enough_information now rather than searching again.`). The tools
learn the count from the agent executing them: each is declared with
Strands' `context=True`, and `note_turn` reads the cycles of the current
invocation from the `ToolContext` — the same number the turn cap compares
against — so the session needs no hook into the agent. Called outside an
agent (a test, a script) there is no context, and a session with no
`max_turns` says nothing about turns.

`enough_information` is refused while nothing has been kept: an answer needs
context, and a run that "succeeds" with none produces no answer at all.

## Reading the outcome

`outcome.note` is one sentence and always names one of five endings. They
mean different things about how complete the selection is, so an
application should show it rather than swallow it:

| Note begins with | Meaning |
|---|---|
| `Agent judged it had enough to answer (…)` | The intended ending. The parenthetical is the agent's own reason |
| `Agent filled its N-chunk maximum` | Stopped by `max_chunks`. The selection may be cut off; raise the limit if answers look truncated |
| `Agent hit the N-iteration cap` | Stopped by `max_iterations` before saying it had enough. What its searches kept is still the selection |
| `Agent stopped without saying it had enough` | The model ended its turn without calling `enough_information`. **This is what a model with weak tool-calling does** — it looks like a finished run but is not one, so the note goes on to say the model is the likely cause |
| `The agent stopped with an error (…)` | An exception mid-run — often the model server refusing the request (model not pulled, or no tool calling for that model). What it had kept so far is returned, and `outcome.error` holds the exception text (it is `""` for every other ending) |

The note ends with how many chunks the judge ignored. With the default pack
every hit a search found carries a verdict; `outcome.undecided` (and a
`left N undecided` in the note) counts chunks shown but never decided, which
only happens with the hand tools or a session run without a judge.

`AgentVaultChat` sends no RAG prompt when nothing was kept — `response` is
then a fixed message rather than an invented answer: `NO_CONTEXT_ANSWER`
says the notes don't appear to answer the question (accurate, since every
hit was judged), and `AGENT_ERROR_ANSWER` replaces it when the run failed
before keeping anything, since then the vault was never searched.

The model matters, though less than it did when the agent kept chunks by
hand: a search keeps what it finds whatever the agent does afterwards. In
testing, `gpt-oss` and `qwen3:8b` run the loop cleanly. `mistral-small`
(the vault's default chat model) tends to end without calling
`enough_information` — the note says so — but its searches have kept the
relevant chunks, so an answer is built. A model that makes no tool calls at
all keeps nothing. All of them end safely.

`minimax-m3.1-flash:cloud` on Ollama plans in its reasoning, and that
reasoning is not sent back to it on the next turn: the Strands Ollama
provider that talkpipe-agents uses drops Ollama's `thinking` field, so each
turn it plans afresh. It used to spend every iteration searching and reading
without ever keeping; now each search keeps for it, so the run hits the
iteration cap with a selection rather than with nothing. Replaying the
reasoning would fix the rest, and that belongs in talkpipe-agents' Ollama
model rather than here. The model is the chat model: on the web and
terminal interfaces, the one chosen under Settings.

## Choosing tools and prompt

Both segments take `tools` and `system_prompt`:

```python
AgentVaultSearch(
    vault_path="~/notes-vault",
    tools="vector_search,enough_information,think",
    system_prompt=MY_PROMPT,
)
```

`tools` is a comma-separated string or a list of names. Names the vault
owns come from `agent_tools.TOOL_FACTORIES`; **any other name is looked up
in talkpipe-agents' registry** (`think` above, or `calculator`,
`current_time`, anything registered with `register_agent_tool`), so generic
tools mix in with no code. An unknown name raises when the segment is built
— for a ChatterLang script, at compile time — with both registries listed.
`None` means the default pack (`agent_tools.default_tool_names()`), in the
order the table above lists it; `keep_chunk` and `ignore_chunk` are opt-in.

Which generic tools suit a retrieval run: the ones that read nothing outside
the vault and change nothing the vault was indexed from — `think`,
`calculator`, `current_time`, `days_between`, `regex_extract`, and the
notepad (`save_note`, `read_notes`, …, which only touch the agent's own
state). `read_file`, `list_files`, and `search_files` read any path the
process can, past the vault's path fences; `fetch_url` leaves the machine;
`ask_user` has no user to ask behind the web interface. None of them is in
the default pack: in trials with qwen3:8b, adding them made the agent keep
the answer less often, not more — every extra tool is one more thing a
small model can pick instead of the search it needed.

`system_prompt` defaults to `agent_search.RETRIEVAL_SYSTEM_PROMPT`. If you
change the tool set, change the prompt to match: the default prompt names
the five default tools and explains when to use each, and a model told about
a tool it does not have will try to call it. The judge's own prompt is
`judge_results.JUDGE_SYSTEM_PROMPT`; the segment takes no override for it,
so subclass `JudgeSearchResults` and rebuild `_generate` if you need one.

One deliberate limit: the web and terminal interfaces only ever request the
default pack. The registry fall-through is for developers and scripts, so a
generic file-reading tool cannot appear behind a checkbox and sidestep the
vault's path fences.

## Using the tools in another application

The tool module has no dependency on the vault. To use the tools over your
own search backend, build a session with your own callables and hand the
tools to any Strands agent — most simply, talkpipe-agents' `llmAgent`. The
judge is the vault's `judgeSearchResults` segment, which needs only a chat
model, so it comes along:

```python
from talkpipe_agents import LLMAgent
from talkpipe_vault.pipelines.agent_search import RETRIEVAL_SYSTEM_PROMPT
from talkpipe_vault.pipelines.agent_tools import (
    AgentSearchSession,
    build_tools,
    parse_tool_names,
)
from talkpipe_vault.pipelines.judge_results import JudgeSearchResults

judge = JudgeSearchResults(chat_source="ollama", chat_model="qwen3:8b")


def my_vector_search(query: str) -> list:
    """Return search hits: talkpipe SearchResult objects, or dicts with
    doc_id, score, and document (a dict with content/title/source)."""
    ...


def my_keyword_search(query: str) -> list:
    ...


def judged(search):
    """A judged search: the request the session builds in, judgements out."""

    def run(request: dict) -> list:
        return judge.judge(
            search(request["query"]),
            request["question"],
            request["looking_for"],
            request["known"],
            request["seen"],
        )

    return run


question = "How did the treasury handle inflation in the 1970s?"
session = AgentSearchSession(
    question=question,
    judged_vector_search=judged(my_vector_search),
    judged_keyword_search=judged(my_keyword_search),
    judge=judged(lambda results: results),  # judges results the session passes
    keyword_index_exists=lambda: True,  # False makes keyword_search say "no index"
    extract_keywords=None,              # or a callable(question) -> keyword query
    max_chunks=10,
    max_turns=8,                        # the agent's cap, so replies count turns down
    tell=print,                         # every tool call and verdict, one line each
)

agent = LLMAgent(
    model="qwen3:8b",
    source="ollama",
    system_prompt=RETRIEVAL_SYSTEM_PROMPT,
    tools=build_tools(session, parse_tool_names(None)),
    max_turns=8,
    progress=session.tell,
)
agent.as_function(single_in=True, single_out=True)(
    f"Question: {question}\nSearch for what answers it; each search keeps its "
    "relevant results. Call enough_information once they would answer it well."
)

session.results        # the kept chunks, as SearchResult, in keep order
session.enough         # True if the agent judged it had enough
session.enough_reason  # its reason
session.trace          # everything passed to `tell`
```

What the session needs from your callables:

- `judged_vector_search` / `judged_keyword_search`: `(request: dict) ->
  iterable of judgements`. The request carries `query`, `question`,
  `looking_for`, `known` (a list of strings, one per kept chunk) and `seen`
  (the identities already decided on: `doc_id`, else the document as JSON).
  A judgement is a dict `{"result": hit, "keep": bool, "reason": str}` — what
  `judgeSearchResults` sets, so `judge.judge(hits, question, looking_for,
  known, seen)` produces them from any hits. A hit is a
  `talkpipe.search.abstract.SearchResult` or a dict shaped `{"doc_id": …,
  "score": …, "document": {...}}`. The document's `content` (or `shingle`)
  is what the judge and the agent read; `title`, `filename`, or `source`
  labels it. Anything else is passed through untouched to `results`. An
  exception from a search is caught and reported to the agent as a failed
  search, not raised.
- `judge`: the same, for `read_document`: the request carries `results` (one
  document part) instead of `query`. Leave it out and the agent is asked to
  keep or ignore the part by hand.
- **Without a judge:** pass `vector_search` / `keyword_search` instead,
  `(query: str) -> iterable of hits`. Their hits are shown to the agent
  undecided, and it decides with `keep_chunk` / `ignore_chunk` — so name
  those in the tool list and give the agent a prompt that tells it to decide
  on every chunk. This is how the pack worked before the judge, and how its
  tests run with no model at all.
- `keyword_index_exists`: `() -> bool`, checked on every keyword search so an
  index built mid-session is picked up.
- `extract_keywords`: `(question: str) -> str` or `None`. With `None` the
  tool tells the agent to write its own query. The vault passes its
  `extractSearchKeywords` segment, which already falls back to the raw
  question when the model fails.
- `read_document`: optional `(source: str) -> str`, the whole text of the
  document a hit records as its `source` (or `path`). Leave it out and the
  `read_document` tool tells the agent reading documents is unavailable.
  The vault's binding refuses a file that is not there and reads the rest
  with talkpipe's extractors.
- `rename_fields`: optional `{"old": "new"}` applied to each document when
  a hit is registered (only when `new` is absent). The vault uses it to turn
  Whoosh's `path`/`filename` into `source`/`title` so keyword hits are
  citable.
- `question`: what the judge judges against. The vault passes the query
  being run; a session built only to resolve tool names passes `""`.
- `max_turns`: optional, the agent's turn cap. Set it to what you give the
  agent (`max_turns=8` in `LLMAgent` above) and the tool replies count the
  turns down; leave it out and they say nothing about turns.

**A session is one query's state.** Build one per query, as the vault
segments do in `make_session(question)`; the segment itself can be shared,
and the web app builds one and serves both pages from it. Within a query,
Strands runs the tool calls of one model step concurrently, in threads. The
session's own bookkeeping allows for that, and `judgeSearchResults` takes
its model calls in turn, but your search callables may be called from
several threads at once, so they must tolerate it.

To reuse the vault's *segments* with your own backend instead — keeping the
outcome, the note, and the `select_chunks`/`answer` split — subclass
`AgentVaultSearch` and override `_bind_vault` (the one construction step that
touches the vault, the embedder, and the models) and the three judged
bindings, each taking the request dict above and returning judgements:

```python
from talkpipe_vault.pipelines.agent_search import AgentVaultSearch, build_agent_model
from talkpipe_vault.pipelines.judge_results import JudgeSearchResults


class MyAgentSearch(AgentVaultSearch):
    def _bind_vault(self) -> None:
        build_agent_model(self.chat_source, self.chat_model)  # keep the up-front check
        self._judge = JudgeSearchResults(chat_model=self.chat_model, chat_source=self.chat_source)
        self._extract_keywords = my_keyword_extractor          # or drop extract_keywords from tools

    def _judged(self, hits, request):
        return self._judge.judge(hits, request["question"], request["looking_for"],
                                 request["known"], request["seen"])

    def judged_vector_search(self, request):
        return self._judged(my_vector_search(request["query"]), request)

    def keyword_index_exists(self): ...

    def judged_keyword_search(self, request):
        return self._judged(my_keyword_search(request["query"]), request)

    def judge(self, request):
        return self._judged(request["results"], request)
```

`vault_path` is still required, but once `_bind_vault` is overridden it is
only a label — nothing opens it. For answers as well as the selection, mix the
subclass into `AgentVaultChat`, which adds the RAG answering stage and no
parameters:

```python
from talkpipe_vault.pipelines.agent_search import AgentVaultChat


class MyAgentChat(MyAgentSearch, AgentVaultChat):
    pass


ask = MyAgentChat(vault_path="my-backend", chat_model="qwen3:8b")
```

This is exactly how the test suite runs the segments without a vault or an
LLM — see `_ScriptedSearch` in `tests/talkpipe_vault/test_agent_search.py`,
which scripts the judgements and additionally replaces `run_agent` with a
script that calls the tools directly.

## Adding a tool to the pack

A vault tool is a factory that takes the session and returns a Strands tool.
Register it with the decorator and it becomes available by name everywhere
`tools=` is accepted, including ChatterLang:

```python
# my_vault_tools.py
from strands import tool as strands_tool
from talkpipe_vault.pipelines.agent_tools import AgentSearchSession, vault_agent_tool, guard


@vault_agent_tool("list_kept")
def make_list_kept(session: AgentSearchSession):
    @strands_tool
    def list_kept() -> str:
        """List the chunks kept so far, with their ids and what each
        contributes, so you can review what the answer will be built
        from before deciding to stop."""
        if refusal := guard(session, "list_kept"):
            return refusal
        session.tell(f"agent · list kept: {len(session.kept)} chunk(s)")
        return "\n".join(session.known_notes()) or "Nothing kept yet."

    return list_kept
```

Three things to keep:

- **The docstring is the tool description the model reads.** Strands takes
  the description from it and the argument schema from the type hints, so
  write it for the model, in the imperative, and document every argument.
- **Start with `guard`.** It returns the refusal once the run is over and
  `""` while it runs; without it the tool keeps working after the agent has
  said it has enough.
- **Report what happened through `session.tell(...)`.** That is the trace the
  interfaces show and the console prints; a tool that acts silently is
  invisible to the person waiting.

Optionally, declare the tool with `@strands_tool(context=True)`, take a
`tool_context: ToolContext | None = None` parameter and call
`note_turn(session, tool_context)` first, as the vault's tools do: then a
run whose only call in a turn is your tool still counts that turn. Strands
keeps the context parameter out of the schema the model sees.

Then add the name to the prompt, or the model will not know it exists — the
simplest way is to extend the default one:

```python
from talkpipe_vault.pipelines.agent_search import RETRIEVAL_SYSTEM_PROMPT, AgentVaultSearch

import my_vault_tools  # registers list_kept

search = AgentVaultSearch(
    vault_path="~/notes-vault",
    chat_model="qwen3:8b",
    tools="vector_search,keyword_search,extract_keywords,read_document,"
    "enough_information,list_kept",
    system_prompt=RETRIEVAL_SYSTEM_PROMPT
    + "\nCall list_kept before enough_information to review what you kept.",
)
```

The tool exists once its module is imported. A ChatterLang script cannot
import it, so name the file when you run the script:
`chatterlang_script --load-module my_vault_tools.py --script '…'`.

## Configuration the vault applies

When the web or terminal interface builds the segment, it passes the chat
source/model from Settings (or TalkPipe configuration), and the two limits
from Settings. Everything else is the default. `_refresh_agent_pipelines` in
`apps/pipelines.py` is the one place that happens; it builds a single
`AgentVaultChat` and hands `select_chunks` to the Search page and the whole
segment to Ask, so the embedder, the judge and the agent model are set up
once. A chat configuration that cannot be run as an agent leaves both pages'
checkboxes hidden — the agent model is resolved eagerly in `_bind_vault` for
exactly that reason.

The vault's retrieval filter (the per-vault ChatterLang script) is **never**
applied to an agent run: the agent searches through its own tools, which the
filter does not see. Both interfaces say so when a filter is active.

## Testing without an LLM

Every tool is a plain function once bound, so the whole loop is testable by
calling them in order. A judged search is a callable that returns
judgements, so script those too:

```python
from talkpipe_vault.pipelines.agent_tools import (
    AgentSearchSession,
    build_tools,
    parse_tool_names,
)

HITS = [{"doc_id": "a", "score": 1.0, "document": {"content": "…", "title": "a.md"}}]
trace: list[str] = []
session = AgentSearchSession(
    question="q",
    judged_vector_search=lambda request: [
        {"result": hit, "keep": True, "reason": "on topic"} for hit in HITS
    ],
    keyword_index_exists=lambda: False, extract_keywords=None,
    max_chunks=5, tell=trace.append,
)
tools = {t.tool_name: t for t in build_tools(session, parse_tool_names(None))}
tools["vector_search"](query="q")
tools["enough_information"](reason="covers it")
assert session.enough and [r.doc_id for r in session.results] == ["a"]
assert session.known_notes() == ["a.md — on topic"]
```

`tests/talkpipe_vault/test_agent_search.py` does this for every tool, every
ending, the refusals, the judged and unjudged paths, and tool selection;
`test_judge_results.py` covers the judge with a scripted model. Neither
needs a vault, embedder, or model, and they run in about a second.
