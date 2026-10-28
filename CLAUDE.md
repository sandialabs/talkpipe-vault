# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding conventions
- When commenting segments or sources, do not include the parameters in the code comment.
  Include a description of the data structure that it expects.
- For sources and segments, parameters should be defined using the Annotated typing convention.
- Commit messages must not include attribution to Claude (no "Co-Authored-By: Claude"
  trailers or "Generated with Claude Code" lines).

## Project Overview

TalkPipe Vault is an AI-powered personal information assistant: a web application for turning
folders of documents into a searchable vault (semantic search, keyword search, RAG Q&A). It is
built on the TalkPipe framework and demonstrates real-world usage of its composable pipelines.

- **Package**: `talkpipe-vault`
- **Module**: `talkpipe_vault`
- **Python**: 3.11.4+ required
- **TalkPipe**: >=1.2.0 (`talkpipe[all]`; ships `py.typed`, so mypy checks against its real signatures)
- **talkpipe-agents**: >=0.1.1 (from PyPI) — the `llmAgent`/Strands layer behind agent search.
  No `py.typed`, so it is in the mypy `ignore_missing_imports` list.
- **Status**: Alpha (active development)
- **License**: Apache 2.0

## Source Code Structure

```
src/talkpipe_vault/
├── __init__.py                 # Package initialization
├── initialize_plugin.py        # talkpipe.plugins entry point (no-op; entry points do the work)
├── watchdog.py                 # File system monitoring (fileWatcher source)
├── apps/
│   ├── query.py                # FastAPI web application (all routes + run_app)
│   ├── vault_server.py         # vault-server CLI entry point (port choice)
│   ├── vault_index.py          # vault-index CLI: runs indexFolder as a ChatterLang script
│   ├── user_settings.py        # Persisted UI settings (recent vaults, model overrides)
│   ├── templates/              # Jinja2 templates (base, home, documents,
│   │                           #   settings, search, keyword_search, chat, partials)
│   └── static/                 # favicon.svg, logo.jpg, icon-256.png + icon.ico (launcher)
├── tui/
│   ├── app.py                  # Textual terminal interface (vault-tui): tabs, dialogs, bindings
│   ├── app.tcss                # Its stylesheet
│   └── service.py              # VaultService: in-process facade over query.py's state/helpers
└── pipelines/
    ├── config.py               # Model/template config resolution + vault layout helpers
    ├── cli.py                  # Experimental watcher CLI helpers (not installed as scripts)
    ├── building_and_watching.py # Experimental watch/batch vector DB pipelines
    ├── search_results.py       # Result shape: normalize, mergeSearchResults, reciprocal rank fusion, filterSearchResults
    ├── keyword_search.py       # searchLance, extractSearchKeywords, keyword-query helpers
    ├── vault_paths.py          # resolve_vault_paths, check_searchable, _SearchesVaultPaths, _IndexView
    ├── vault_search.py         # vaultSearch, vaultTextSearch and the fan-out searches
    ├── vault_chat.py           # vaultChat
    ├── searching_and_prompting.py # Original import path: re-exports the five modules above
    ├── folder_files.py         # Folder mode's shared rules: index location, document files, change tracker, file diff
    ├── folder_manifest.py      # Manifest: a folder index's per-file state (sqlite)
    ├── folder_scheduler.py     # FolderScheduler: the background indexing thread
    ├── folder_index.py         # FolderIndex, open_folder helpers, indexFolder/watchFolders
    ├── folder_changes.py       # folderChanges: a folder's document files as a stream of changes
    ├── agent_tools.py          # The vault's agent tool pack (session + bound-tool factories)
    └── agent_search.py         # vaultAgentSearch / vaultAgentChat, built on llmAgent
```

## The Web Application (primary user path)

`vault-server [folder|vault_path] [--dir FOLDER ...] [--as-vault] [--resume] [--host] [--port] [--show-source-paths] [--no-browser]` —
`--resume` opens the most recently used vault (fallback: vault_path); the vault path is
optional; without it the last session's folders reopen, else the UI starts on the
Vaults & Documents page.

Startup (`apps/vault_server.py`): if a vault already serves the port (checked
through the unauthenticated `GET /api/health`, which reports `DIST_NAME` and
the version), the browser is opened at it and the process exits; if some other
program holds 8002 and no `--port` was given, the next free port in 8003–8022
is used and announced; an explicit `--port` that is taken fails before the
banner. The packaged `apps/static/icon-256.png` and `icon.ico` exist for desktop
launchers.

Routes in `apps/query.py`:
- `/documents` (+ `/documents/index`): the combined **Vaults & Documents** page — pick the
  documents to index *and* the vault to index them into, in one submit. Works with no
  vault open: `/api/suggest-vault` proposes a vault name derived from the chosen documents
  folder (`suggest_vault_path`), and `/documents/index` creates/opens that vault before
  indexing. Indexing uses TalkPipe's `build_rag_database()` driver (writes the
  `docs` table — the same driver behind TalkPipe's `makevectordatabase` CLI, with
  embedder preflight, dimension checking, overflow truncation, and skip counting).
- `/vaults/open` and `/vaults/delete` back the recent-vaults list on that page; recents are
  persisted via `user_settings.py` in `$TALKPIPE_VAULT_HOME` (default `~/.talkpipe-vault`).
  `GET /vaults` is a redirect alias for `/documents`.
- `/settings`: embedding + chat source/model overrides, persisted and applied immediately.
- `/`, `/search`, `/keyword-search` (+ index creation), `/chat`, `/chunk-content`,
  `/source-file`, `/open-file` (streams the source document behind a result/citation,
  resolved by lookup key — works without `--show-source-paths`), `/refresh`.
- Pages that need a vault redirect to `/documents` when none is selected.

State lives in the module-level `_state: AppState` singleton; pipelines are rebuilt by
`_refresh_pipelines()` (throttled to every 5s unless forced).

## The Terminal Interface

`vault-tui [vault_path] [--resume] [--show-source-paths]` (`talkpipe_vault/tui/`) is a
Textual application with the web interface's functionality for terminals (tmux, SSH,
headless machines). It runs **in-process**: `tui/service.py` (`VaultService`) calls the
same module-level helpers the routes in `apps/query.py` call (`init_pipelines`,
`_refresh_pipelines`, `start_index_job`, `_process_semantic_results`, `_collect_config_status`,
`_resolve_vault_request`, …) on the same `_state` singleton, so both interfaces share one
implementation and the web application is not modified. Keep it that way: a TUI feature
that needs new logic gets a helper in `query.py` (or a pipelines module) that both
interfaces call — never TUI-only copies of route logic. Every service method is
synchronous and blocking; the app runs them in `@work(thread=True)` workers and updates
widgets via `call_from_thread`. Tests (`tests/talkpipe_vault/test_tui_*.py`) exercise the
service against a real docs-table vault and the app with Textual's `Pilot`. Bindings for
tabs/quit are `priority=True` (TextArea claims keys), labels are plain text (emoji width is
unreliable in terminals), and the screen gets a `compact` class under 26 rows. The Ask
answer is rendered with Textual's `Markdown` widget (LLM output is Markdown: tables,
headings, emphasis); "Copy answer" copies the model's source text, kept in
`_answer_text`, and non-Markdown prose shown in that widget (errors, the initial hint)
goes through `_plain_text_as_markdown` so it appears verbatim.

## Model Configuration Precedence (highest to lowest)

1. Explicit parameters passed to segments/sources
2. Web-interface Settings page (`$TALKPIPE_VAULT_HOME/settings.json`)
3. TalkPipe configuration (`~/.talkpipe.toml` or `TALKPIPE_*` env vars; also accepts
   `default_embedding_model_name`/`default_model_name` style keys)
4. Defaults in `pipelines/config.py`: model2vec/minishlab/potion-retrieval-32M (embeddings, in-process),
   ollama/mistral-small (chat)

**Providers are whatever TalkPipe registers** — nothing here is Ollama-specific. The
Settings page/tab dropdowns are `getEmbeddingSources()` / `getPromptSources()`, i.e.
`model2vec`, `ollama`, `openai` for embeddings and `ollama`, `openai`, `anthropic`,
`eliza` for chat, plus anything an installed plugin registers; `pipelines/diagnostics.py`
has a tailored check per provider and probes unrecognized ones by using them.
`apps/credentials.py` persists `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`
and `TALKPIPE_OLLAMA_SERVER_URL` into the process environment (stored values win over
pre-existing ones). `eliza` is a scripted responder, not a model — plumbing checks only.
User-facing docs: README "LLM providers" is the authoritative section; `docs/ADVANCED.md`
"Provider notes" the reference.

Ollama server URL comes from `TALKPIPE_OLLAMA_SERVER_URL` (or `OLLAMA_SERVER_URL` in
`~/.talkpipe.toml`) — not `OLLAMA_BASE_URL`, which is meaningless to TalkPipe.

**Path fences** (`apps/access_control.py`): `TALKPIPE_VAULT_ROOT` confines vault
create/open/delete to one directory; `TALKPIPE_DOCUMENT_ROOTS` (os.pathsep-separated)
confines the folder picker and indexing. Unset/empty = unrestricted (the desktop
default); the container image sets `/app/data` and `/documents`. Enforced server-side
in `/api/directories`, `/vaults/open`, `/vaults/delete`, and `/documents/index` via
`access_control.confine` (realpath-then-prefix-check, so symlink escapes are caught;
the confined result, never the raw request path, is what touches the filesystem — with
no fence set the check runs against the filesystem root); a configured root that
doesn't exist fails startup loudly, and `run_app` warns when binding non-loopback with
no fences set. Deleting a remembered vault that lies outside the root only forgets it —
files outside the fence are never touched.

**Per-vault embedding restore:** embeddings are only comparable to a query embedded
with the same model, so the embedder is a property of the indexed data, not just a
preference. Indexing records `embedding_source`/`model`/`dimension` to
`vault_metadata.json` (see Vault Layout), and opening a vault applies that recorded
embedder over the Settings-page override (`init_pipelines` → `_apply_vault_embedding_config`).
Legacy vaults with no record leave the current setting unchanged and are flagged in the
Settings configuration status (`diagnostics._check_embedding_index_match`). Only embedding
config is data-coupled and restored this way — chat config stays a free preference, and
server URLs / API keys are resolved fresh at run time (a server URL is stored only as a
non-authoritative `indexed_via_url` breadcrumb).

## Vault Layout

- LanceDB lives directly at `vault_path` (same semantics as `makevectordatabase --path`);
  the web app reads the `docs` table (`DEFAULT_VECTOR_TABLE_NAME`).
- Whoosh full-text index lives at `vault_path/fulltext_vault`.
- `vault_path/vault_metadata.json` records the embedding config the vault was indexed with
  (`pipelines/vault_metadata.py`); it travels with the vault if copied/moved. Best-effort —
  a missing or unreadable file is treated as a legacy vault, never an error.
- A legacy `vault_path/vector_vault` layout is rejected with migration guidance
  (`ensure_supported_vault_layout`).
- **Folder mode** keeps a folder's index — an ordinary vault in this layout — in
  `<folder>/.tpvault/`, plus `manifest.db` (sqlite, per-file state, written only by the
  lock holder), `status.json` (progress for other processes), `writer.lock`,
  `.gitignore` (`*`) and `CACHEDIR.TAG`. Read-only, network, synced folders and a set
  `TALKPIPE_VAULT_ROOT` put it under `<vault root or home>/folder-indexes/` instead
  (`resolve_index_location`). Never add `.tpvault` to the vault markers in
  `_existing_non_vault_entries`: that would make the documents folder count as a vault.
- The experimental watcher pipelines in `building_and_watching.py` write different tables
  (`full_documents`, `shingled_chunks`) that the web app does NOT read; they are not part
  of the stable path and are not installed as console scripts.

## TalkPipe Sources/Segments (registered entry points)

Sources: `fileWatcher`, `watchIntoVectorDB`, `listIntoVectorDB` (watcher ones experimental),
`watchFolders`, `folderChanges`.
Segments: `buildVectorDBFromPaths`, `vaultSearch`, `vaultChat`, `vaultTextSearch`,
`searchLance`, `extractSearchKeywords`, `mergeSearchResults`, `filterSearchResults`,
`vaultAgentSearch`, `vaultAgentChat`, `indexFolder`.

## Folder mode

Users point at folders instead of creating vaults. The engine lives in four
`pipelines/` modules that depend on nothing in `apps/` except
`default_index_home()` (lazy import); keep it that way so `indexFolder`,
`watchFolders` and `folderChanges` stay usable in other applications.

- `folder_files.py`: what both components share. `resolve_index_location`
  (in the folder, else app data for read-only, network, synced folders or a
  set `TALKPIPE_VAULT_ROOT`), `document_files` (hidden paths, temporary
  files and out-of-folder symlinks skipped; only extractor-readable
  extensions), `diff_files` (known-versus-disk comparison: stat, then hash,
  so a touched file is "touched" not "changed"), `ChangeTracker` (watcher
  events debounced, periodic rescans; `due()` returns the subtrees to
  compare) and `watch`. The watcher drops events with any hidden path
  component, since watchdog's `.*` ignore matches only the last component.
- `folder_manifest.py`: `Manifest`, the writer's per-file state in
  `manifest.db` (`pending` → `keyword` → `done`, or `error`).
- `folder_scheduler.py`: `FolderScheduler`, one daemon thread per process:
  scans, then keyword work for every folder, then vectors; `ingest.lock` in
  the vault home serializes batches across processes. Maintenance
  (`reembed`, `rebuild`) and closing a folder run on the worker through
  `submit()` / `remove()`.
- `folder_index.py`: `FolderIndex` is one folder. `try_become_writer()`
  takes `writer.lock` (filelock, an OS lock released on any exit); without it
  the folder is a reader showing the writer's `status.json`. `scan()`
  applies `diff_files` to the manifest (changed and deleted files lose their
  chunks from both stores at once), `keyword_batch()` chunks pending files
  into Whoosh, `vector_window()` embeds stored chunks via
  `build_rag_database(chunks=…)`. Chunk ids are
  `{uuid8}-{sha1(relpath)[:16]}:{n:05d}` in both stores, so redone batches
  replace rather than duplicate. Mark a file done only after the store call
  returns. `status()["finished"]` is true for a complete folder and for one
  whose scan found nothing to index; the interfaces' "still indexing" notices
  key off it, not `complete`.
- `folder_changes.py` (`folderChanges`) builds on `folder_files` and shares
  no state with the indexer: it reads no contents (pipelines use talkpipe's
  `readFile`) and keeps a per-name record in `<folder>/.tpstream/<name>.db`
  (`stream_state_path`; app data where the folder cannot hold it), apart from
  `.tpvault` so an index rebuild never re-sends a folder. A file is recorded
  only when the consumer asks for the next item (at-least-once); keep it
  that way.

Three LanceDB operations the vault needs (prefix delete, table drop,
raw-distance search) have no public `LanceDBDocumentStore` method yet, so
`_docs_table`, `_drop_docs_table` and `_FanOutVectorSearch._search` use its
internals; a talkpipe issue asks for them. Remove the private calls when it
lands.

**Search** (`vault_search.py`, `vault_chat.py`, `vault_paths.py`; the old
`searching_and_prompting.py` re-exports everything for existing imports and
must gain a name whenever one of the five modules gains a public one):
`VaultSearch`, `VaultTextSearch`,
`VaultChat` and `AgentVaultSearch` accept a list of paths; `resolve_vault_paths`
maps a documents folder to its index and `needs_fan_out` keeps a single named
vault on its old pipeline. The three segments share `_SearchesVaultPaths`:
options are attributes, what depends on the paths is built in `_build(paths)`,
and `_ensure_searchable()` (called before every query) runs `_build` again when
the indexes behind `vault_path` changed, then `check_searchable` refuses a
missing path or a documents folder with no index. `VaultChat._build` has one
pipeline shape: retrieval stages added as the options ask, then RAG prompt →
completion → sources. Fan-out groups indexes by recorded embedder (distance
merge inside a group, `reciprocal_rank_fusion` across), tags hits with
`folder`, rebases sources when a folder moved, and makes its embedders on
first use, skipping indexes with no vectors yet (`vectors_started`).
`keyword_fallback` adds keyword hits for folders still embedding.

**App glue** (`apps/query.py`, "Folder mode" block): `add_folder`,
`close_folder`, `open_closed_folder`, `remove_folder` (open or closed),
`reembed_folder`, `rebuild_folder` (all under `_folders_lock`),
`folders_status` (open folders' progress, then closed folders as
`closed: True` entries that nothing waits on), `_folder_banner`,
`_partial_index_note`,
and the lookups both interfaces use: `chunk_text`, `resolve_source_file`
(folder mode serves only a stored chunk's own path, never the request value).
`startup_folders` resolves what a command line opens for both `vault-server`
and `vault-tui`, and refuses a vault path together with `--dir`. Notices both
interfaces show are worded once (`SEMANTIC_SEARCH_WAITING_TEMPLATE`,
`KEYWORD_SEARCH_STARTING`, `FOLDERS_INDEX_THEMSELVES_TEMPLATE`,
`FOLDERS_KEEP_EMBEDDER_TEMPLATE`); the TUI fills in its page names. In folder
mode `_state.folders` lists the roots and `_state.vault_path` is the first
folder's index; folder and named-vault modes are exclusive. Routes:
`/folders/add`, `/folders/close`, `/folders/open`, `/folders/remove`,
`/folders/reembed`, `/folders/rebuild`;
`/api/index-status` carries `folders` and `banner`. Named-vault index runs
hold the vault's `writer.lock` (ignored by `existing_non_vault_entries`) and
pause the folder indexer (`_index_run_guard`). Settings: `open_folders`,
`closed_folders`, `recent_folders`, `last_session`, `index_location`. Tests use the offline
`fake_embedder` fixture (`tests/conftest.py`, source `fakehash`);
`docs/DEVELOPER_GUIDE.md` shows every component in Python and ChatterLang,
and every block there was run; keep it so. `docs/ADVANCED.md` is for operators
(Advanced Usage); component documentation goes in the Developer Guide.

**Agent retrieval** is split in two, deliberately: `pipelines/agent_tools.py` owns *what
the tools do*, `pipelines/agent_search.py` owns *the pipeline that runs them*. Nothing in
the tool module knows about agents, models, or prompts, so the pack is what would move out
if these tools were ever pulled into a generic package. `docs/AGENT_RETRIEVAL.md` is the
developer's guide — using the tools over another search backend, behind another agent,
adding a tool, reading the outcome; keep it current when either module changes.

`AgentVaultSearch.__init__` does everything pure first (parse tools, defaults, validate
tool names) and then calls `_bind_vault()`, the one step that touches the outside world
(the vault, the embedder, the keyword-extraction model, and `build_agent_model` as the
eager check that hides the checkboxes on a bad chat configuration). Tests subclass and
override `_bind_vault` rather than bypassing the constructor, so a new attribute cannot go
missing from the test double. `AgentVaultChat` adds no `__init__` at all — its RAG
pipeline is a `cached_property` — so there is one parameter list, not two to keep in sync.
The Ask route and `VaultService.ask` share `query._answer_with_agent` and the
`AGENT_*_NOTE` constants; do not re-inline that branch in either.

`agent_tools.py` — seven tools, all needing per-query state, so each is a **factory**
returning a tool bound to one `AgentSearchSession` (the pattern talkpipe-agents documents
for stateful packs): `vector_search`, `keyword_search`, `extract_keywords`,
`read_document`, `keep_chunk`, `ignore_chunk`, `enough_information`. `read_document` takes
a chunk id, never a path: it reads the `source` that chunk records (through the session's
injected `read_document` callable; `AgentVaultSearch.read_document` uses talkpipe's
`ReadFile`, i.e. the indexing extractors) and registers the part read as a new chunk to
keep or ignore. `@vault_agent_tool("name")` registers a factory in
`TOOL_FACTORIES`, mirroring talkpipe-agents' own name registry; `build_tools(session,
names)` resolves a name list against it and **falls through to `resolve_agent_tools` for
anything it does not own**, so a pipeline can mix in generic tools (`think`, `calculator`,
…) by name with no code change here. An unknown name raises at construction — compile time
for a ChatterLang script. That fall-through is a developer/ChatterLang escape hatch: the
interfaces only ever request the default pack, so a generic file-reading tool cannot turn
up behind a checkbox and sidestep the path fences. Hits are assigned short ids (`c1`,
`c2`, …; deduped by doc_id across searches) and the agent decides on each.

`agent_search.py` — `vaultAgentSearch` **does not build a Strands agent**; it configures
talkpipe-agents' `llmAgent` segment directly and reads how the run ended from
`agent.last_result.stop_reason` (the Strands result the segment keeps). Model resolution,
tool dispatch, progress reporting and reply cleanup are all the library's. Both the tool list
(`tools="vector_search,keep_chunk,…"`, a string or a list) and the `system_prompt` are
segment parameters, so tools and agents mix and match without editing the module; the
default is the whole vault pack with `RETRIEVAL_SYSTEM_PROMPT`. `make_session` /
`make_agent` / `run_agent` / `select_chunks` and the `vector_search` / `keyword_search` /
`extract_keywords` bindings are the overridable seams; tests drive the tools with scripted
callables and no LLM.

**The stopping criterion is the agent's own judgement that it could answer the question
well from what it kept, not a result count** — it says so by calling `enough_information`
(refused while nothing is kept: an answer needs context), which sets `session.enough` and
its reason. `max_chunks` (Settings → Agent Chunk Limit, default `DEFAULT_MAX_CHUNKS`) is a
*ceiling* on collected context and `max_iterations` (Settings → Agent Search Iterations)
is passed straight through as `llmAgent`'s `max_turns`; both only bound a run that never
reaches that judgement. **The stop is enforced by the tools, not by an agent hook**: every
tool starts with `_guard`, which refuses once `session.should_stop` (`enough or
at_capacity`) and says which — so the rule holds behind any agent, and there is no
`BeforeToolCallEvent` callback of ours to keep in step with the library's. The note
distinguishes all four endings — judged
it had enough (with the reason), filled the chunk maximum, hit the iteration cap, or
stopped without saying it had enough — because each means something different about how
complete the selection is; the last is what weaker tool-calling models do. The outcome is an
`AgentSearchOutcome(results, note, trace)`: kept chunks as `SearchResult` in keep order, a
note on how it ended, and the tool-call trace, which is also printed (`progress`, stdout
by default) so the agent's behavior is visible on the server console; a keyword-only
`error` holds the exception text when the run failed (`""` otherwise). `extract_keywords` is the LLM
keyword distillation the Ask page's keyword boost uses (the `ExtractSearchKeywords`
segment): the agent hands it a plain-language question and gets back a Whoosh query to
pass to `keyword_search`, rather than having to write Whoosh syntax itself. The agent
never builds the full-text index — when a vault has none, `keyword_search` says so and
the agent falls back to `vector_search`; building the index stays a user action on the
Keyword Search page. `AgentVaultSearch.vector_search` / `keyword_search` /
`vaultAgentChat` (`AgentVaultChat`) subclasses it for Ask: it calls `select_chunks` and
feeds the kept chunks to the same `ConstructRAGPrompt` → `LLMPrompt` → `AppendRAGSources`
stages `vaultChat` ends with, so an agent-gathered answer is worded and cited exactly like
a plain one — only the retrieval in front differs. It emits an `AgentChatOutcome` (an
`AgentSearchOutcome` plus `response`); when the agent kept nothing, no prompt is sent and
`NO_CONTEXT_ANSWER` is returned instead of asking a model to answer from an empty context
(`AGENT_ERROR_ANSWER` when the run failed before keeping anything — the vault was never
searched, so the answer must not blame it; `UNDECIDED_ANSWER` when the agent left chunks it
found undecided — counted in the outcome's `undecided` and the note's `left N undecided` —
since it never judged them). `_bind_vault` refuses a vault path that does not
exist, so a typo fails at construction instead of creating an empty folder. Strands runs one
turn's tool calls concurrently in threads, so `AgentSearchSession._register` assigns chunk ids
under a lock; an unknown chunk id passed to keep/ignore is refused *and* traced.

**Both pages are driven by one segment.** `_refresh_agent_pipelines` builds a single
`AgentVaultChat` per refresh and sets `_state.agent_search_pipeline = segment.select_chunks`
and `_state.agent_chat_pipeline = segment.as_function(...)` — one embedder and one agent
model, not two. The model is resolved eagerly, so a bad chat configuration leaves *both*
unset and both checkboxes hidden (Search: **Let an agent select the results**; Ask:
**Use Agent**) — the web pages show `_state.agent_unavailable_reason` (the build error)
in the checkbox's place — in the web interface and in the TUI (`#search-agent` / `#ask-agent`, shown
from `status()["agent_search_available"]` / `["agent_chat_available"]`). Both interfaces
render the trace in an "Agent tool calls" panel. Ask's **Use Agent** takes precedence over
the keyword boost (the agent already has keyword search as a tool), and a requested-but-
unavailable agent is reported in the "Answered by" line rather than silently ignored.
The vault's retrieval filter is *not* applied to an agent run — the agent searches through
its own tools, which the filter never sees — and both pages say so when a filter is active.

**Keyword-augmented Ask** (issue #11): `vaultChat[keyword_search=True]` composes the
reusable pipeline prompt → `extractSearchKeywords` (LLM distills the question into a
quoted Whoosh OR-query; falls back to the raw question on failure) → `vaultTextSearch`
→ vector search → `mergeSearchResults` (normalizes both hit shapes to SearchResult,
dedupes by doc_id — the Whoosh index reuses LanceDB row ids — and renames
path/filename → source/title so keyword hits are citable) → RAG prompt → completion.
The Ask page shows a "Boost retrieval with keyword search" checkbox when the vault
has a Whoosh index; `_refresh_pipelines` keeps a separate `keyword_chat_pipeline`
and Ask degrades to the plain pipeline when it can't be built. The answer's
"Answered by" meta line reports the keyword-boost outcome (applied with N keyword
hits, applied but no extra hits, or unavailable — degradation is never silent),
fed by the `keyword_hits` count `include_background` adds to the pipeline result
(issue #19: dedup against vector hits made the boost look like it was ignored).

## Development Commands

```bash
# Setup — local development resolves against the committed uv.lock.
# CI installs with pip on purpose, so it resolves the way a plain
# `pip install talkpipe-vault` does and breaks when users would break.
# uv.lock is a development convenience, not a security control: fix a
# vulnerable dependency by raising its floor in pyproject.toml, and re-run
# `uv lock` after any dependency change so `uv lock --check` stays green.
uv sync --extra dev

# Tests (Ollama-dependent tests skip unless a server is reachable; point at a
# remote server with TALKPIPE_OLLAMA_SERVER_URL — never commit a real URL/IP)
pytest
TALKPIPE_OLLAMA_SERVER_URL=http://<ollama-host>:11434 pytest

# Quality (CI fails on any finding from these -- no advisory mode)
ruff check .           # lint; fix most findings with: ruff check --fix .
ruff format --check .  # formatting; apply with: ruff format .
mypy                   # static types over src/ (config in pyproject.toml)
# `pre-commit install` (opt-in, per clone) runs the same three on every
# commit; `pre-commit run --all-files` reproduces the CI gate locally.

# Security. safety is in no extra: it caps filelock below talkpipe's floor,
# so it runs in its own environment, from an empty directory (CI does the same).
bandit -r src/
pip freeze --exclude-editable | (cd "$(mktemp -d)" && uvx safety check --stdin)
```

Test notes:
- `tests/conftest.py` provides `build_docs_vault()` (builds a docs-table vault via
  `python -m talkpipe.app.makevectordatabase` with explicit embedding flags — required
  since TalkPipe 0.12.4) and `is_ollama_available()`.
- Web-app tests use FastAPI's TestClient against `query.app` and monkeypatch
  `TALKPIPE_VAULT_HOME` to a tmp dir so real user settings are never touched. Follow that
  pattern for new UI tests.

## Container

```bash
podman build --build-arg APP_VERSION="$(python3 -m setuptools_scm)" -t talkpipe-vault -f Containerfile .
podman run --rm -p 8002:8002 --userns=keep-id \
    -v <host-data-dir>:/app/data:Z -v <host-docs-dir>:/documents:ro,Z \
    -e TALKPIPE_OLLAMA_SERVER_URL=http://host.containers.internal:11434 \
    talkpipe-vault
```

Default CMD runs `vault-server --resume` on port 8002 (no default vault): it
reopens the vault last used in the web interface, starting on the Vaults & Documents
page on the first run before any vault has been opened. Mount documents somewhere readable
(e.g. `/documents`) and index them from that page. Compose services (`vault`,
`vault-dev`) exist in docker-compose.yml; with podman, install a compose provider
(`pip install podman-compose`) and run `podman compose up -d`.

Podman notes:
- With rootless podman (pasta networking), browse to `http://127.0.0.1:8002`, not
  `localhost` — ports publish IPv4-only and pasta resets `::1` connections instead
  of refusing them, so name resolution to `::1` fails without fallback.
- The compose services mount `$VAULT_DOCUMENTS_DIR` (default `~/Documents`)
  read-only at `/documents` — the folder picker browses the *container*
  filesystem, so host files are only visible through this mount.
- The image sets `HF_HOME=/app/data/hf-cache`, so the embedding model is
  downloaded once, on first use, into the data volume and survives container
  recreation — keep `/app/data` on a volume (`-v <host-data-dir>:/app/data`,
  as the command above and the compose services do) or the model is
  re-downloaded every recreation.
- `container/entrypoint.sh` probes huggingface.co at startup when
  `HF_HUB_OFFLINE` is unset and switches to offline (cache-only) model loads
  if it is unreachable, so nothing hangs on connection timeouts; set
  `HF_HUB_OFFLINE=1`/`0` in `.env` to force a mode. If the model cannot be
  loaded at all (unreachable and not yet cached), the server still starts —
  without a vault, with a warning on stderr and diagnostics on the Settings
  page — and downloads the model once it starts with connectivity again.

## CI/CD

GitHub Actions (`.github/workflows/ci-cd.yml`), analogous to TalkPipe's pipeline:
1. Lint: ruff check, ruff format --check, mypy -- all gating
2. Test (Python 3.11/3.12/3.13): pytest+coverage, Codecov
3. Security scan: Bandit + Safety (depends on lint and test)
4. Container build: build/push to GHCR + Trivy scan
5. CodeQL analysis
6. Publish to PyPI on release

## Key Configuration Files

- `pyproject.toml`: package config, entry points, tool settings
- `Containerfile` / `docker-compose.yml`: container image and services
- `.env.example`: example container environment (model/provider settings and
  credentials; compose passes a service only the variables it names under
  `environment:`, so configuring a provider that way under compose means
  adding it there — the Settings page needs no environment variables)
- `.github/workflows/ci-cd.yml`: CI/CD pipeline
