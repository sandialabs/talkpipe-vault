# Agent retrieval — developer's guide

How the vault's agent-driven retrieval is built, and how to use its pieces in
your own application: with a different search backend, a different agent, a
different set of tools, or none of the vault at all.

The machinery is talkpipe-agents' **findings loop**
([its guide](https://github.com/sandialabs/talkpipe-agents/blob/main/docs/findings.md)):
tools that return *findings*, a judge that decides on every finding as it
arrives, and a per-question session that keeps what is worth keeping and
ends the run. The vault keeps only what is the vault's, in three modules:

| Module | Owns | Knows about |
|---|---|---|
| `talkpipe_vault/pipelines/judge_results.py` | **The judge as a segment** — `judgeSearchResults`, talkpipe-agents' `FindingsJudge` over search results, and `search_findings`, the adapter from search hits to findings | A chat model and the search-result shape. Nothing about agents, tools, or vaults |
| `talkpipe_vault/pipelines/agent_tools.py` | **What the tools do** — the vault's four retrieval tools (`vector_search`, `keyword_search`, `read_document` as finding tools; `extract_keywords` as a plain one) | The search callables you hand it, as a `SearchBackend`. Nothing about agents, models, prompts, or vaults |
| `talkpipe_vault/pipelines/agent_search.py` | **The pipeline that runs them** — the `vaultAgentSearch` / `vaultAgentChat` segments | The vault on disk, the embedder, the judge, the retrieval prompt, the RAG answer, talkpipe-agents' `llmAgent` |

The session (`FindingsSession`), the hook that judges inside the agent loop
(`JudgeFindings`), and the session tools (`enough_information`,
`keep_finding`, `ignore_finding`) are talkpipe-agents', used as they come.

If you want the judge after your own search, take the first module. If you
want the vault's tools behind another agent, take the second. If you want
the vault's behaviour with a different agent model or tool set, keep all
three and change a parameter. All of these are shown below.

## The sixty-second version

In Python:

```python
from talkpipe_vault.pipelines.agent_search import AgentVaultChat

ask = AgentVaultChat(
    vault_path="~/notes",  # a folder opened in vault-server, or a named vault
    chat_source="ollama",
    chat_model="gpt-oss:latest",  # a model that handles tool calls well — see below
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
| vaultAgentChat[vault_path="~/notes-vault", chat_source="ollama", chat_model="gpt-oss:latest"]
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

The agent is talkpipe-agents' `llmAgent` segment, with the findings loop's
`JudgeFindings` hook installed over a fresh `FindingsSession` for the
question. It is given five tools by default:

| Tool | What it does |
|---|---|
| `vector_search(query, looking_for)` | Semantic search; the hits come back judged, each with a short id (`f1`, `f2`, …) |
| `keyword_search(query, looking_for)` | Full-text search (Whoosh syntax), judged the same way; says so if the vault has no index |
| `extract_keywords(question)` | Asks an LLM to turn a plain-language question into a keyword query |
| `read_document(finding_id, start, looking_for)` | Loads the whole document a finding came from, as a new finding judged like a hit; long documents come in `DOCUMENT_PART_CHARS` (6000-character) parts, each labelled with the characters it covers, `start` picking which |
| `enough_information(reason)` | **How a run ends:** the agent's judgement that what has been kept would answer the question well (talkpipe-agents' session tool) |

Two more can be named in `tools=` — talkpipe-agents' `keep_finding(finding_id,
reason)` and `ignore_finding(finding_id, reason)`, the decisions by hand, for
a run without a judge (see [Using the tools behind another
agent](#using-the-tools-behind-another-agent)). Their old names,
`keep_chunk` and `ignore_chunk`, still resolve to them.

**Each search's hits are judged inside the agent loop.** `vector_search`
calls `vaultSearch` and `keyword_search` calls `vaultTextSearch`; each turns
its hits into findings (`search_findings`) and returns them. The
`JudgeFindings` hook then takes them, after the call and before the model
sees the result, and the judge
([Developer Guide: Judging results](DEVELOPER_GUIDE.md#judging-results-judgesearchresults))
decides on all of them in one batched model call, so it costs the agent no
turn and cannot be skipped. It sees the user's question, what the agent said
this search is `looking_for`, and what is already known — one line per kept
finding, its title and what it contributes — and keeps a hit that helps
answer the question even in part, or that supplies what the search was
looking for, provided it adds something to what is known. It ignores a hit
that only shares words with the question, a near-duplicate, or something
that reads a word in the question as another thing with the same name. When
unsure, it keeps. Hits already decided on are skipped, not judged again;
`max_chunks` is respected in rank order, and a hit judged worth keeping once
the maximum is full is passed over and the agent told.

So the reply the model reads is the session's: the kept findings in full,
one line per ignored finding with the reason, the current "already known"
notes, and the status. The agent's job is what to search for next — to say
in `looking_for` what the answer still lacks (a fact, a date, the other side
of a comparison, a corroborating document), using terms from the kept
findings, and to stop when what is known would answer the question. Findings
are deduplicated across searches by key — a hit's `doc_id`, falling back to
the document — so a chunk found twice keeps its id and its verdict. The
judge and the agent see up to `CHUNK_PREVIEW_CHARS` (1000) characters of
each finding, enough for a whole chunk at the default indexing settings.

`read_document` is for a hunch that a finding's document holds more of the
answer — the chunk is cut off, it is one step of a procedure, or its
document is plainly where the topic is covered. The agent names a finding it
has seen, never a path, so it can only read documents the vault itself
returned, and the vault reads them with the extractors indexing used. The
part it reads is one finding, judged the same way (`looking_for` saying what
the agent hopes it holds); a kept part reaches the answer, and its citation
opens the document, like any other. A document that is no longer where it
was indexed is reported to the agent, not an error of the run.

A tool that cannot do what it was asked — an empty query, no full-text
index, an unknown finding id, a document that cannot be read — raises, and
the agent reads the message as the tool's error result; the trace shows it
as `agent ← tool: error: …`.

**The stopping criterion is the agent's judgement, not a count.** A run ends
when the agent calls `enough_information`. Two limits exist only to bound a
run that never gets there: `max_chunks` (the most chunks one run may keep;
Settings → *Agent Chunk Limit*) and `max_iterations` (agent turns; Settings
→ *Agent Search Iterations*; the Search and Ask pages' *Extra Effort* box
doubles it for one question, via `with_max_iterations`, a copy of the
segment that shares its vault, model bindings and tools). Once either the
judgement is made or the chunk ceiling is full, every tool refuses further
calls with the same short message telling the model to stop and reply
`done` — the session's guard, which the finding tools check before running,
so it holds behind any agent.

The agent is told its turn budget. The opening prompt says how many turns it
has, and every tool reply ends with how many are left (`5 of 8 turns
left.`), the last one spelled out (`1 turn left: the next one is your last,
so call enough_information now rather than another tool.` — or, while
nothing is kept, `… so try once more with different terms, or answer that
nothing relevant was found.`, since `enough_information` would be refused).
The hook reads the count from the agent before every tool call — the same
number the turn cap compares against.

`enough_information` is refused while nothing has been kept: an answer needs
context, and a run that "succeeds" with none produces no answer at all.

The vault installs the hook itself (`make_agent` passes
`hooks=[JudgeFindings(session, judge)]` to `llmAgent`) rather than using
`llmAgent[judge=True]`, for two reasons: the agent only gathers, so the
session's `stop_instruction` tells it to reply `done` rather than to answer
(the answer is built afterwards, from the kept chunks), and the vault reads
the session to word the outcome.

## Reading the outcome

`outcome.note` is one sentence and always names one of six endings. They
mean different things about how complete the selection is, so an
application should show it rather than swallow it:

| Note begins with | Meaning |
|---|---|
| `Agent judged it had enough to answer (…)` | The intended ending. The parenthetical is the agent's own reason |
| `Agent filled its N-chunk maximum` | Stopped by `max_chunks`. The selection may be cut off; raise the limit if answers look truncated |
| `Agent hit the N-iteration cap` | Stopped by `max_iterations` before saying it had enough. What its searches kept is still the selection |
| `Agent found nothing relevant to keep` | It searched, the judge ignored every hit, and it stopped — the right ending for a question the notes do not cover (`enough_information` is refused with nothing kept). Ask answers with `NO_CONTEXT_ANSWER` |
| `Agent stopped without saying it had enough` | The model ended its turn without calling `enough_information` while it had kept something, or without searching at all. **This is what a model with weak tool-calling does** — it looks like a finished run but is not one, so the note goes on to say the model is the likely cause |
| `The agent stopped with an error (…)` | An exception mid-run — often the model server refusing the request (model not pulled, or no tool calling for that model). What it had kept so far is returned, and `outcome.error` holds the exception text (it is `""` for every other ending) |

These are the findings loop's endings (`FindingsSession.outcome`), worded
for the pages: chunks, searches (calls of the search tools and
`read_document`), iterations. The note ends with how many chunks the judge
ignored. With the default pack every hit a search found carries a verdict;
`outcome.undecided` (and a `left N undecided` in the note) counts chunks
shown but never decided, which only happens in a run without a judge.

`AgentVaultChat` sends no RAG prompt when nothing was kept — `response` is
then a fixed message rather than an invented answer: `NO_CONTEXT_ANSWER`
says the notes don't appear to answer the question (accurate, since every
hit was judged), and `AGENT_ERROR_ANSWER` replaces it when the run failed
before keeping anything, since then the vault was never searched.

The model matters, though less than it did when the agent kept chunks by
hand: a search keeps what it finds whatever the agent does afterwards. In
testing, `gpt-oss:latest` and `qwen3:8b` run the loop cleanly. `mistral-small`
(the vault's default chat model) tends to end without calling
`enough_information` — the note says so — but its searches have kept the
relevant chunks, so an answer is built. A model that makes no tool calls at
all keeps nothing. All of them end safely.

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
in talkpipe-agents' registry** (`enough_information` and `think` above, or
`calculator`, `current_time`, anything registered with
`register_agent_tool` or `@finding_tool`), so generic tools mix in with no
code. An unknown name raises when the segment is built — for a ChatterLang
script, at compile time — with both registries listed. `None` means the
default pack (`agent_tools.default_tool_names()`), in the order the table
above lists it.

Which generic tools suit a retrieval run: the ones that read nothing outside
the vault and change nothing the vault was indexed from — `think`,
`calculator`, `current_time`, `days_between`, `regex_extract`, and the
notepad (`save_note`, `read_notes`, …, which only touch the agent's own
state). `read_file`, `list_files`, and `search_files` read any path the
process can, past the vault's path fences; `fetch_url` leaves the machine;
`ask_user` has no user to ask behind the web interface. None of them is in
the default pack: in trials with qwen3:8b, adding them made the agent keep
the answer less often, not more — every extra tool is one more thing a
small model can pick instead of the search it needed. A plain tool's reply
gets the turn countdown appended too; a finding tool's results are judged
like a search's.

`system_prompt` defaults to `agent_search.RETRIEVAL_SYSTEM_PROMPT`. If you
change the tool set, change the prompt to match: the default prompt names
the five default tools and explains when to use each, and a model told about
a tool it does not have will try to call it. The judge's own prompt is
talkpipe-agents' `JUDGE_SYSTEM_PROMPT` (re-exported by `judge_results`); to
change it, give the judge's `LLMPrompt` another system prompt in a subclass
of `JudgeSearchResults`.

One deliberate limit: the web and terminal interfaces only ever request the
default pack. The registry fall-through is for developers and scripts, so a
generic file-reading tool cannot appear behind a checkbox and sidestep the
vault's path fences.

## Using the tools behind another agent

The tool module has no dependency on the vault. To use the tools over your
own search backend, build them on a `SearchBackend` of your own callables,
and run them under the findings loop: a `FindingsSession` for the question,
the `JudgeFindings` hook over it, and the judge. talkpipe-agents' `llmAgent`
takes the hook in `hooks=`; any Strands `Agent` takes it the same way.

```python
from talkpipe.search.abstract import SearchResult
from talkpipe_agents import FindingsSession, JudgeFindings, LLMAgent
from talkpipe_vault.pipelines.agent_search import RETRIEVAL_SYSTEM_PROMPT, STOP_INSTRUCTION
from talkpipe_vault.pipelines.agent_tools import SearchBackend, build_tools, parse_tool_names
from talkpipe_vault.pipelines.judge_results import JudgeSearchResults

NOTES = {
    "inflation.md": "In the 1970s the treasury fought inflation with wage and price controls.",
    "volcker.md": "From 1979 the Federal Reserve raised interest rates sharply to end inflation.",
    "garden.md": "Tomatoes and basil grow well together.",
}


def my_search(query: str) -> list[SearchResult]:
    """Return search hits: talkpipe SearchResult objects, or dicts with
    doc_id, score, and document (a dict with content/title/source)."""
    words = {w.lower().strip("?.,") for w in query.split() if len(w) > 3}
    return [
        SearchResult(score=1.0, doc_id=name, document={"content": text, "title": name})
        for name, text in NOTES.items()
        if words & {w.lower().strip("?.,") for w in text.split()}
    ]


backend = SearchBackend(
    vector_search=my_search,
    keyword_search=my_search,
    keyword_index_exists=lambda: True,  # False makes keyword_search say "no index"
    extract_keywords=None,  # or a callable(question) -> keyword query
)
tools = build_tools(backend, parse_tool_names(None))

question = "How did the treasury handle inflation in the 1970s?"
session = FindingsSession(
    question,  # what the judge judges against
    max_kept=10,
    max_turns=8,  # the agent's cap, so replies count turns down
    tell=print,  # every tool call and verdict, one line each
    stop_instruction=STOP_INSTRUCTION,  # "reply: done" -- we answer later
)
judge = JudgeSearchResults(chat_source="ollama", chat_model="gpt-oss:latest").findings_judge

agent = LLMAgent(
    model="gpt-oss:latest",
    source="ollama",
    system_prompt=RETRIEVAL_SYSTEM_PROMPT,
    tools=tools,
    max_turns=8,
    progress=session.tell,
    hooks=[JudgeFindings(session, judge)],
)
agent.as_function(single_in=True, single_out=True)(
    f"Question: {question}\nSearch for what answers it; each search keeps its "
    "relevant results. Call enough_information once they would answer it well."
)

[f.to_search_result() for f in session.kept]  # the kept hits, in keep order
session.enough  # True if the agent judged it had enough
session.enough_reason  # its reason
session.trace  # everything passed to `tell`
```

What the tools need from the backend (any of it may be `None`; the tool that
needs it then tells the agent it is unavailable):

- `vector_search` / `keyword_search`: `(query: str) -> iterable of hits`. A
  hit is a `talkpipe.search.abstract.SearchResult` or a dict shaped
  `{"doc_id": …, "score": …, "document": {...}}`. The document's `content`
  (or `text`, `shingle`) is what the judge and the agent read; `title` (or
  `filename`) labels it and `source` (or `path`) is what it cites and what
  `read_document` reads. Anything else is passed through untouched to the
  kept hit. An exception from a search is reported to the agent as a failed
  search.
- `keyword_index_exists`: `() -> bool`, checked on every keyword search so an
  index built mid-session is picked up.
- `extract_keywords`: `(question: str) -> str`. With `None` the tool tells
  the agent to write its own query. The vault passes its
  `extractSearchKeywords` segment, which already falls back to the raw
  question when the model fails.
- `read_document`: `(source: str) -> str`, the whole text of the document a
  hit records as its `source`. The vault's binding refuses a file that is
  not there and reads the rest with talkpipe's extractors.
- `rename_fields`: `{"old": "new"}` applied to each hit's document (only
  when `new` is absent). The vault uses it to turn Whoosh's
  `path`/`filename` into `source`/`title` so keyword hits are citable.

The callables may be called from several threads at once: Strands runs the
tool calls of one model step concurrently. The session and the judge allow
for that (the hook holds the session's lock for a whole judgement, so a
second call's hits are judged against what the first kept).

**Without a judge:** pass `JudgeFindings(session)` with no judge. A
search's findings are then shown to the agent undecided, with their ids, and
it decides with `keep_finding` / `ignore_finding` — so name those in the
tool list and give the agent a prompt that tells it to decide on every
finding.

To reuse the vault's *segments* with your own backend instead — keeping the
outcome, the note, and the `select_chunks`/`answer` split — subclass
`AgentVaultSearch` and override `_bind_vault` (the one construction step that
touches the vault, the embedder, and the models) and the search bindings the
tools call: `vector_search(query)`, `keyword_search(query)`,
`keyword_index_exists()`, `extract_keywords(question)` and
`read_document(source)`, each a plain callable as above:

```python
from talkpipe_vault.pipelines.agent_search import AgentVaultChat, AgentVaultSearch, build_agent_model
from talkpipe_vault.pipelines.judge_results import JudgeSearchResults


class MyAgentSearch(AgentVaultSearch):
    def _bind_vault(self) -> None:
        build_agent_model(self.chat_source, self.chat_model)  # keep the up-front check
        self._judge = JudgeSearchResults(chat_model=self.chat_model, chat_source=self.chat_source)

    def vector_search(self, query):
        return my_search(query)

    def keyword_index_exists(self):
        return True

    def keyword_search(self, query):
        return my_search(query)

    def extract_keywords(self, question):
        return question  # or drop extract_keywords from tools


class MyAgentChat(MyAgentSearch, AgentVaultChat):
    pass


ask = MyAgentChat(vault_path="my-backend", chat_source="ollama", chat_model="gpt-oss:latest")
print(ask.process_value("How did the treasury handle inflation in the 1970s?").response)
```

`vault_path` is still required, but once `_bind_vault` is overridden it is
only a label — nothing opens it. `MyAgentChat` mixes the subclass into
`AgentVaultChat`, which adds the RAG answering stage and no parameters.
`findings_judge` (a property returning the `JudgeSearchResults` segment's
judge) is the judge the run uses; override it to judge differently, or to
return `None` for a run without one.

This is how the test suite runs the segments without a vault or an LLM —
see `_Scripted` in `tests/talkpipe_vault/test_agent_search.py`, which also
replaces the agent's model with talkpipe-agents' `ScriptedModel` (the
`scripted_model` pytest fixture) and the judge with its `ScriptedJudge`.

## Adding a tool to the pack

A tool that finds things is a **finding tool**: an ordinary function that
returns findings — `Finding` objects, strings, dicts with `text`/`label`/
`source`, or search hits — made a tool with talkpipe-agents'
`@finding_tool`. It knows nothing about sessions or judges: under the loop,
its findings are judged and kept like a search's, and once the run is over
it is refused without running. `@finding_tool` registers it by name, so it
is available everywhere `tools=` is accepted, including ChatterLang:

```python
# my_vault_tools.py
from talkpipe_agents import Finding, finding_tool

GLOSSARY = {
    "stagflation": "High inflation together with stagnant growth and high unemployment.",
}


@finding_tool
def glossary(term: str, looking_for: str = "") -> list[Finding]:
    """Look up a term in the team glossary. Use it when the question uses
    a term of art whose meaning matters to the answer.

    Args:
        term: The term to look up.
        looking_for: What this lookup should establish, in one phrase.
    """
    text = GLOSSARY.get(term.strip().lower())
    return [Finding(text, label=f"glossary: {term}", source="glossary")] if text else []
```

A vault tool that needs the search backend is a factory instead, registered
in `TOOL_FACTORIES` with `@vault_agent_tool("name")`: it takes the
`SearchBackend` and returns the tool (see `make_vector_search` in
`agent_tools.py`). Either way:

- **The docstring is the tool description the model reads.** Strands takes
  the description from it and the argument schema from the type hints, so
  write it for the model, in the imperative, and document every argument.
- **Take `looking_for`** if the tool finds things: the judge keeps a finding
  that supplies what the agent said it was looking for.
- **Raise when the tool cannot do what it was asked.** The agent reads the
  message as the tool's error result, and the trace shows it.

A finding a tool returns as a `Finding` (rather than a search hit) reaches
the answer through `Finding.to_search_result()`, which builds a hit from its
text, label and source, so it is cited by its `source`.

Then add the name to the prompt, or the model will not know it exists — the
simplest way is to extend the default one:

```python
from talkpipe_vault.pipelines.agent_search import RETRIEVAL_SYSTEM_PROMPT, AgentVaultSearch

import my_vault_tools  # registers glossary

search = AgentVaultSearch(
    vault_path="~/notes-vault",
    chat_model="gpt-oss:latest",
    tools="vector_search,keyword_search,extract_keywords,read_document,"
    "enough_information,glossary",
    system_prompt=RETRIEVAL_SYSTEM_PROMPT
    + "\nUse glossary when the question uses a term of art.",
)
```

The tool exists once its module is imported. A ChatterLang script cannot
import it, so name the file when you run the script:
`chatterlang_script --load-module my_vault_tools.py --script '…'`.

**With no Python at all**, `findingTool` makes a ChatterLang script a finding
tool: every item the script emits is a finding, judged and kept like a
search hit. This adds a judged search over a second vault to the vault's
agent, from a script file, with a prompt that names it:

```sh
cat > two_vaults.cl <<'EOF'
findingTool[name="search_papers",
            description="Semantic search over my papers. Pass a question or descriptive phrase.",
            script="| vaultSearch[vault_path='/path/to/papers', limit=5] | flatten"];
INPUT FROM echo[data="What share of traffic does a canary release get first?"]
| vaultAgentChat[vault_path="/path/to/notes", chat_source="ollama", chat_model="gpt-oss:latest",
                 tools="vector_search,search_papers,enough_information",
                 system_prompt="You gather what answers the question; you do not answer it. vector_search searches my notes and search_papers my papers, and each reply keeps the relevant results. Search both. Call enough_information once the kept findings would answer the question, then reply: done"]
| extractProperty[property="response"]
| print
EOF
chatterlang_script --script two_vaults.cl
```

The tool takes `text` (the script's input) and `looking_for` (passed to the
judge). `vaultSearch` emits one list of hits per query, so `flatten` makes
each hit a finding of its own, with its title and a source to cite. The script compiles afresh per call, so it holds no state; the
session the loop runs is what remembers what was kept.

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

talkpipe-agents' `ScriptedModel` replays the agent's tool calls through the
real agent loop, and `ScriptedJudge` decides on findings by rule, so the
whole loop runs offline, the real tools included:

```python
from talkpipe_agents import FindingsSession, JudgeFindings, LLMAgent
from talkpipe_agents.testing import ScriptedJudge, scripted_source
from talkpipe_vault.pipelines.agent_tools import SearchBackend, build_tools, parse_tool_names

HITS = [{"doc_id": "a", "score": 1.0, "document": {"content": "…", "title": "a.md"}}]
tools = build_tools(SearchBackend(vector_search=lambda q: HITS), parse_tool_names(None))
session = FindingsSession("q", max_kept=5)

with scripted_source() as model:
    model.call("vector_search", query="q").call("enough_information", reason="covers it")
    model.say("done")
    agent = LLMAgent(
        source="scripted",
        model="any",
        tools=tools,
        hooks=[JudgeFindings(session, ScriptedJudge(keep={"a.md"}))],
    )
    agent.as_function(single_in=True, single_out=True)("q")

assert session.enough and [f.to_search_result().doc_id for f in session.kept] == ["a"]
assert session.known_notes() == ["a.md — relevant"]
```

`tests/talkpipe_vault/test_agent_search.py` does this for every tool, every
ending, the refusals, the judged and unjudged paths, and tool selection;
`test_judge_results.py` covers the judge segment with a scripted model.
Neither needs a vault, embedder, or model, and they run in about a second.
