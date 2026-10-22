# Agent retrieval — developer's guide

How the vault's agent-driven retrieval is built, and how to use its pieces in
your own application: with a different search backend, a different agent, a
different set of tools, or none of the vault at all.

Everything described here lives in two modules, split on purpose:

| Module | Owns | Knows about |
|---|---|---|
| `talkpipe_vault/pipelines/agent_tools.py` | **What the tools do** — the six retrieval tools and the per-query state they share | Search callables you hand it. Nothing about agents, models, prompts, or vaults |
| `talkpipe_vault/pipelines/agent_search.py` | **The pipeline that runs them** — the `vaultAgentSearch` / `vaultAgentChat` segments | The vault on disk, the embedder, the RAG prompt, talkpipe-agents' `llmAgent` |

If you want the tools in another application, you take the first module and
leave the second. If you want the vault's behaviour with a different agent,
you keep both and change one parameter. Both are shown below.

## The sixty-second version

In Python:

```python
from talkpipe_vault.pipelines.agent_search import AgentVaultChat

ask = AgentVaultChat(
    vault_path="~/notes-vault",
    chat_source="ollama",
    chat_model="qwen3:8b",  # a model that handles tool calls well — see below
).as_function(single_in=True, single_out=True)
outcome = ask("How did the treasury handle inflation in the 1970s?")

outcome.response   # the answer, with a "Sources:" list appended
outcome.results    # the chunks it was built from, as talkpipe SearchResult, in keep order
outcome.note       # one line: how the run ended (see "Reading the outcome")
outcome.trace      # every tool call and decision, as printed to the console
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
`ollama` / `mistral-small` — which, as [Reading the outcome](#reading-the-outcome)
explains, rarely keeps what it finds. A script does **not** see the model
chosen on the web interface's Settings page, so pass `chat_model` (and
`chat_source`) or set the configuration. The other parameters
(`max_chunks`, `max_iterations`, `embedding_model`, …) are listed, with what
each does, by `help(AgentVaultSearch)`.

`vaultAgentSearch` is the same thing without the answering stage: it emits an
`AgentSearchOutcome` (results, note, trace) and is what the Semantic Search
page uses. `vaultAgentChat` subclasses it, and takes exactly the same
parameters plus nothing — the answer is generated from whatever the search
kept.

## How a run works

The agent is talkpipe-agents' `llmAgent` segment. It is given six tools, all
bound to one `AgentSearchSession` that holds that query's state:

| Tool | What it does |
|---|---|
| `vector_search(query)` | Semantic search; returns chunks with short ids (`c1`, `c2`, …) |
| `keyword_search(query)` | Full-text search (Whoosh syntax); says so if the vault has no index |
| `extract_keywords(question)` | Asks an LLM to turn a plain-language question into a keyword query |
| `keep_chunk(chunk_id, reason)` | The chunk contributes to the answer |
| `ignore_chunk(chunk_id, reason)` | It does not |
| `enough_information(reason)` | **How a run ends:** the agent's judgement that what it kept would answer the question well |

Chunks are deduplicated across searches by `doc_id` (falling back to the
document's content), so a chunk found twice keeps its id and its decision.
The agent is told to decide on every chunk it sees — keeping one that would
help answer even part of the question, and keeping it when unsure, since an
ignored chunk is lost to the answer — to refine its queries from what it
learns, and — after each search — to ask what the question still lacks and
search for that. It sees up to `CHUNK_PREVIEW_CHARS` (1000) characters of
each chunk, enough for a whole chunk at the default indexing settings.

**The stopping criterion is the agent's judgement, not a count.** A run ends
when the agent calls `enough_information`. Two limits exist only to bound a
run that never gets there: `max_chunks` (the most chunks one run may keep;
Settings → *Agent Chunk Limit*) and `max_iterations` (agent turns; Settings
→ *Agent Search Iterations*). Once either the judgement is made or the chunk
ceiling is full, every tool refuses further calls with the same short message
telling the model to stop — the rule is enforced inside the tools, so it
holds behind any agent, not just the one the vault builds.

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
| `Agent hit the N-iteration cap` | Stopped by `max_iterations` before deciding |
| `Agent stopped without saying it had enough` | The model ended its turn without calling `enough_information`. **This is what a model with weak tool-calling does** — it looks like a finished run but is not one, so the note goes on to say the model is the likely cause |
| `The agent stopped with an error (…)` | An exception mid-run — often the model server refusing the request (model not pulled, or no tool calling for that model). What it had kept so far is returned, and `outcome.error` holds the exception text (it is `""` for every other ending) |

`AgentVaultChat` sends no RAG prompt when nothing was kept — `response` is
then a fixed "nothing to answer from" message rather than an invented answer
(`NO_CONTEXT_ANSWER`, or `AGENT_ERROR_ANSWER` when the run failed before
keeping anything, since then the vault was never searched).

The model matters. In testing, `gpt-oss` and `qwen3:8b` run the loop
cleanly; `mistral-small` (the vault's default chat model) searches but rarely
calls `keep_chunk`; some models make no tool calls at all. All of them end
safely (the weak ones as "stopped without saying it had enough" or "nothing
to answer from"), but only the first kind is useful. The model is the chat
model: on the web and terminal interfaces, the one chosen under Settings.
Trace lines like `agent ✗ keep_chunk refused: unknown chunk id 'c1'` are the
signature of a model that keeps chunks before reading them — guessing ids, or
sending the keep in the same step as the search that would return them.

## Choosing tools and prompt

Both segments take `tools` and `system_prompt`:

```python
AgentVaultSearch(
    vault_path="~/notes-vault",
    tools="vector_search,keep_chunk,ignore_chunk,enough_information,think",
    system_prompt=MY_PROMPT,
)
```

`tools` is a comma-separated string or a list of names. Names the vault
owns come from `agent_tools.TOOL_FACTORIES`; **any other name is looked up
in talkpipe-agents' registry** (`think` above, or `calculator`,
`current_time`, anything registered with `register_agent_tool`), so generic
tools mix in with no code. An unknown name raises when the segment is built
— for a ChatterLang script, at compile time — with both registries listed.
`None` means the whole vault pack, in the order the table above lists it.

`system_prompt` defaults to `agent_search.RETRIEVAL_SYSTEM_PROMPT`. If you
change the tool set, change the prompt to match: the default prompt names
all six tools and explains when to use each, and a model told about a tool
it does not have will try to call it.

One deliberate limit: the web and terminal interfaces only ever request the
default pack. The registry fall-through is for developers and scripts, so a
generic file-reading tool cannot appear behind a checkbox and sidestep the
vault's path fences.

## Using the tools in another application

The tool module has no dependency on the vault. To use the tools over your
own search backend, build a session with your own callables and hand the
tools to any Strands agent — most simply, talkpipe-agents' `llmAgent`:

```python
from talkpipe_agents import LLMAgent
from talkpipe_vault.pipelines.agent_search import RETRIEVAL_SYSTEM_PROMPT
from talkpipe_vault.pipelines.agent_tools import (
    AgentSearchSession,
    build_tools,
    parse_tool_names,
)


def my_vector_search(query: str) -> list:
    """Return search hits: talkpipe SearchResult objects, or dicts with
    doc_id, score, and document (a dict with content/title/source)."""
    ...


def my_keyword_search(query: str) -> list:
    ...


session = AgentSearchSession(
    vector_search=my_vector_search,
    keyword_search=my_keyword_search,
    keyword_index_exists=lambda: True,  # False makes keyword_search say "no index"
    extract_keywords=None,              # or a callable(question) -> keyword query
    max_chunks=10,
    tell=print,                         # every tool call and decision, one line each
)

agent = LLMAgent(
    model="qwen3:8b",
    source="ollama",
    system_prompt=RETRIEVAL_SYSTEM_PROMPT,
    tools=build_tools(session, parse_tool_names(None)),
    max_turns=8,
    progress=session.tell,
)
question = "How did the treasury handle inflation in the 1970s?"
agent.as_function(single_in=True, single_out=True)(
    f"Question: {question}\nSearch, keep the chunks the answer needs, and call "
    "enough_information once they would answer it well."
)

session.results        # the kept chunks, as SearchResult, in keep order
session.enough         # True if the agent judged it had enough
session.enough_reason  # its reason
session.trace          # everything passed to `tell`
```

What the session needs from your callables:

- `vector_search` / `keyword_search`: `(query: str) -> iterable of hits`. A
  hit is a `talkpipe.search.abstract.SearchResult` or a dict shaped
  `{"doc_id": …, "score": …, "document": {...}}`. The document's `content`
  (or `shingle`) is what the agent reads; `title`, `filename`, or `source`
  labels it. Anything else is passed through untouched to `results`. An
  exception from a search is caught and reported to the agent as a failed
  search, not raised.
- `keyword_index_exists`: `() -> bool`, checked on every keyword search so an
  index built mid-session is picked up.
- `extract_keywords`: `(question: str) -> str` or `None`. With `None` the
  tool tells the agent to write its own query. The vault passes its
  `extractSearchKeywords` segment, which already falls back to the raw
  question when the model fails.
- `rename_fields`: optional `{"old": "new"}` applied to each document when
  a hit is registered (only when `new` is absent). The vault uses it to turn
  Whoosh's `path`/`filename` into `source`/`title` so keyword hits are
  citable.

**A session is one query's state.** Build one per query, as the vault
segments do in `make_session()`; the segment itself can be shared, and the
web app builds one and serves both pages from it. Within a query, Strands runs
the tool calls of one model step concurrently, in threads. The session's
own bookkeeping allows for that, but your search callables may be called from
several threads at once, so they must tolerate it.

To reuse the vault's *segments* with your own backend instead — keeping the
outcome, the note, and the `select_chunks`/`answer` split — subclass
`AgentVaultSearch` and override `_bind_vault` (the one construction step that
touches the vault, the embedder, and the models) and the three bindings:

```python
from talkpipe_vault.pipelines.agent_search import AgentVaultSearch, build_agent_model


class MyAgentSearch(AgentVaultSearch):
    def _bind_vault(self) -> None:
        build_agent_model(self.chat_source, self.chat_model)  # keep the up-front check
        self._extract_keywords = my_keyword_extractor          # or drop extract_keywords from tools

    def vector_search(self, query): ...
    def keyword_index_exists(self): ...
    def keyword_search(self, query): ...
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
which additionally replaces `run_agent` with a script that calls the tools
directly.

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
        """List the chunks kept so far, with their ids, so you can review
        what the answer will be built from before deciding to stop."""
        if refusal := guard(session, "list_kept"):
            return refusal
        session.tell(f"agent · list kept: {len(session.kept)} chunk(s)")
        return "\n".join(f"[{cid}]" for cid in session.kept) or "Nothing kept yet."

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

Then add the name to the prompt, or the model will not know it exists — the
simplest way is to extend the default one:

```python
from talkpipe_vault.pipelines.agent_search import RETRIEVAL_SYSTEM_PROMPT, AgentVaultSearch

import my_vault_tools  # registers list_kept

search = AgentVaultSearch(
    vault_path="~/notes-vault",
    chat_model="qwen3:8b",
    tools="vector_search,keyword_search,extract_keywords,keep_chunk,"
    "ignore_chunk,enough_information,list_kept",
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
`apps/query.py` is the one place that happens; it builds a single
`AgentVaultChat` and hands `select_chunks` to the Search page and the whole
segment to Ask, so the embedder and the agent model are set up once. A chat
configuration that cannot be run as an agent leaves both pages' checkboxes
hidden — the agent model is resolved eagerly in `_bind_vault` for exactly
that reason.

The vault's retrieval filter (the per-vault ChatterLang script) is **never**
applied to an agent run: the agent searches through its own tools, which the
filter does not see. Both interfaces say so when a filter is active.

## Testing without an LLM

Every tool is a plain function once bound, so the whole loop is testable by
calling them in order:

```python
from talkpipe_vault.pipelines.agent_tools import (
    AgentSearchSession,
    build_tools,
    parse_tool_names,
)

HITS = [{"doc_id": "a", "score": 1.0, "document": {"content": "…", "title": "a.md"}}]
trace: list[str] = []
session = AgentSearchSession(vector_search=lambda q: HITS, keyword_search=lambda q: [],
                             keyword_index_exists=lambda: False, extract_keywords=None,
                             max_chunks=5, tell=trace.append)
tools = {t.tool_name: t for t in build_tools(session, parse_tool_names(None))}
tools["vector_search"](query="q")
tools["keep_chunk"](chunk_id="c1", reason="on topic")
tools["enough_information"](reason="covers it")
assert session.enough and [r.doc_id for r in session.results] == ["a"]
```

`tests/talkpipe_vault/test_agent_search.py` does this for every tool, every
ending, the refusals, and tool selection; it needs no vault, embedder, or
model and runs in about a second.
