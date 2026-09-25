# Changelog

## In Development

### Terminal interface
- `vault-tui` no longer stalls on quit. Every service call (a search, an
  Ask, a settings probe) runs on a pool thread that stays alive, idle, after
  the call returns, and the exit path counted each of those idle threads as
  "still blocked" and waited its full two-second grace period for it in
  turn — so `Ctrl+Q` took two seconds per pool thread the session had ever
  started (four seconds right after startup, longer on machines with more
  cores or after a session of searches and asks) and looked like a hang
  that needed `Ctrl+C`. The pool is now shut down as the interface closes,
  which releases idle threads at once; only a thread actually inside a
  service call is waited for, the grace period is shared across such
  threads rather than paid per thread, and `Ctrl+C` during it skips
  straight to leaving.
- An unticked checkbox no longer looks ticked. Textual draws the "X" inside
  an unchecked box in a dark colour on a dark box; it stayed legible, so
  **Overwrite existing index**, **Boost with keyword search** and **Clear the
  saved key** all read as already on — and on a terminal without 24-bit
  colour the two states were identical. The mark is now genuinely absent when
  off and bold when on.

### Added

- **Packaged icons.** The package now ships `icon-256.png` and `icon.ico`
  (cropped from the logo) under `apps/static/`, for desktop launchers.
- **Second launch opens the running instance.** Starting `vault-server` —
  or clicking the launcher — while it is already running opens the browser
  at the running server instead of failing on the busy port, using the new
  `GET /api/health` route, which reports the application name and version
  and works before any vault is opened.
- **Free-port fallback.** When port 8002 is held by some other program and
  no `--port` was given, the server uses the next free port in the
  8003–8022 range and announces it. An explicit port that is taken now
  fails before the banner with a clear message instead of a uvicorn
  traceback afterwards.

### Documentation

- **Provider-neutral documentation.** The README, Advanced Guide,
  `.env.example`, and compose comments no longer read as if Ollama were
  required. A new "LLM providers" section in the README is the single place
  that lists what vault can use — model2vec, Ollama or OpenAI for
  embeddings; Ollama, OpenAI or Anthropic for chat, plus any provider a
  TalkPipe plugin registers — with how to select one and where its API key
  or server URL goes; the quickstart, container, terminal-interface, and
  requirements sections link to it instead of repeating Ollama-only advice.
  The guide also spells out the two ways to configure a provider in a
  container: on the Settings page, which persists in the data volume, or
  from the environment — naming the variable under a compose service's
  `environment:` key (or an `env_file`), since compose passes a service
  only the variables it names.
- **Keyword Search's one-off index is now in the quickstart.** The README
  presented all three ways of exploring a vault as working the moment
  indexing finished, but Keyword Search starts disabled until its separate
  full-text index is built from the page's own button — a step the README
  mentioned only in passing, inside the terminal-interface section. The
  quickstart and the web-interface list now say so, including that indexing
  more documents later does not refresh that index.
- **`vault-tui` is no longer described as unreleased.** It has shipped on
  PyPI since 1.0.0, so the terminal-interface section no longer sends readers
  through a `git+https://…` install to get it.
- **The "install from source" instruction now names the branch.** The
  repository's default branch is the release-only `stable`, so the
  `git+https://…` URL offered for getting ahead of the PyPI release installed
  exactly the release. It is now spelled `…talkpipe-vault.git@master`, with
  the reason, and the Advanced Guide's development setup checks out `master`
  after cloning.
- **The vault-name suggestion is documented correctly.** The Advanced Guide
  said an unfenced install suggests a vault "next to" the documents folder; it
  suggests `~/<folder-name>-vault`.
- The Advanced Guide's OpenAI configuration examples no longer name `gpt-4`.

### Fixed

- `talkpipe_vault.__version__` reported a hard-coded `0.1.0`; it now
  reports the installed distribution's version.
- The Settings page's configuration status described eliza as a "rule-based
  responder", which read like a working chat provider. It now says the
  replies do not use your documents and that a model provider is needed for
  real answers.
- **A bare `host:port` typed into the web Settings page is completed to
  `http://host:port`,** as it already was in the terminal interface. The web
  page stored the value as typed, reported "Connection settings saved", and
  then failed every request with an unsupported-scheme error. A URL that can
  never work is now refused at the field instead of being saved, and both
  interfaces share one normalization step.
- **The first screen no longer looks like an error.** Opening the app with no
  vault redirected to the Vaults & Documents page with "Choose the documents
  to index to get started." in the red alert style, announced as an alert —
  a welcome message dressed as a failure. It is now a neutral status message.
- **The Semantic Search result count no longer comes from outside the
  application.** That page's pipeline left its result limit unset and
  inherited whatever default the vector store shipped. It now passes the
  application's own limit, as the filtered variant already did. Ask's citation
  list retrieves enough rows to show the configured Ask result count when
  that is set above ten.
- **The "Ask result count" setting said it controlled Semantic Search.** It
  does not — that page always shows its top ten matches. The field is now
  labelled "Ask & keyword result count" in both interfaces and its help text
  says what it actually governs.
- **Container environment variables that nothing read are gone.**
  `VAULT_PATH`, documented in `.env.example` and set by the Containerfile and
  both compose services, was presented as the way to choose which vault the
  container serves; no code read it, so editing it did nothing and said
  nothing. `VAULT_HOST` was likewise inert for the published image, whose CMD
  fixes the host and port. Both are removed, and the files now explain that
  `--resume` plus the web interface decide which vault is open.
- **The compose documents mount works under Docker Compose.** Its default was
  `~/Documents`, and no compose provider expands `~` in a volume mapping —
  Docker Compose treated it as a relative path while podman-compose happened
  to expand it, so the same file behaved differently on the two providers the
  header claims to support. `VAULT_DOCUMENTS_DIR` is now required and must be
  absolute, and the services fail immediately with a message saying so
  instead of coming up with nothing to index.
- **The compose healthcheck can fail.** It ran `import talkpipe_vault`, which
  succeeds whether or not the web application is listening, so an unhealthy
  container was never restarted. It now requests `GET /api/health`.
- **Better hints when a search finds nothing, and when Ask has no provider.**
  The Keyword Search page's empty-result suggestions now mention that very
  common words are not indexed and point at the rebuild button, rather than
  suggesting a shorter query for a one-word search. The Semantic Search
  results note explains that the closest ten chunks are always returned, so
  weak matches at the end of the list are expected. A provider error on the
  Ask page now leads with the Settings page rather than offering it as a tip
  after two environment-variable suggestions.

## 1.0.0 (2026-08-29)

First stable release. TalkPipe Vault turns folders of documents into a
searchable vault — semantic search, exact keyword search, and RAG question
answering ("Ask") over your own files — with a web interface and a terminal
interface over the same engine. It runs fully locally by default: embeddings
are computed in-process with no server or API key, and chat answers use
whichever provider you configure (Ollama by default; OpenAI, Anthropic, and
a keyless smoke-test responder are supported).

The 0.0.x releases were previews, so this section describes the application
as it stands at 1.0.0 rather than itemizing every change since them; the
full development history is in the git log. Changes that require action when
upgrading from a 0.0.x install are listed at the end.

### What ships in 1.0.0

- **Web interface** (`vault-server`): a Vaults & Documents page that creates
  or opens a vault and indexes a documents folder into it in one submit,
  with a server-side folder picker, live indexing progress, and a remembered
  recent-vaults list; Semantic Search and Keyword Search pages with
  full-chunk and source-document viewers and copy buttons; and an Ask page
  whose answers cite their sources and carry an "Answered by" line stating
  exactly how the answer was produced.
- **Terminal interface** (`vault-tui`): the web interface's functionality as
  a Textual application for tmux, SSH sessions, and machines without a
  browser. It runs in-process — no `vault-server` needed — against the same
  state, pipelines, settings, and credentials as the web app, and fits an
  80×24 terminal. New dependency: `textual`.
- **Settings from the UI** (both interfaces): embedding and chat
  provider/model choices, chunking and retrieval sizes, and connection
  credentials (Ollama server URL, OpenAI/Anthropic API keys, an
  OpenAI-compatible base URL for vLLM/LM Studio/llama.cpp), with a live
  configuration-status panel that probes each provider and suggests fixes.
  Choices persist under `~/.talkpipe-vault` (`TALKPIPE_VAULT_HOME`).
- **Keyword-boosted Ask**: when the vault has a full-text index, Ask can
  distill the question into a keyword query and merge those hits with
  vector retrieval; the answer's meta line reports whether the boost
  applied and how many keyword hits it contributed.
- **Per-vault retrieval filters** (advanced): a ChatterLang script stored
  with the vault (`retrieval_filter.tps`) filters or transforms retrieved
  results before they reach Ask or the search pages. Whether it runs is a
  per-machine, per-vault choice — a vault received from someone else never
  executes its bundled script just by being opened — and an optional Strict
  mode makes a failing filter fail closed for filters that remove sensitive
  content.
- **Per-vault embedding restore**: indexing records the embedding
  source/model to `vault_metadata.json`, and opening a vault restores that
  embedder over the current settings — so a vault is always searched with
  the model it was indexed with, even after the default or the settings
  change.
- **Path fences for shared deployments**: `TALKPIPE_VAULT_ROOT` confines
  vault create/open/delete to one directory and `TALKPIPE_DOCUMENT_ROOTS`
  confines the folder picker and indexing; every request-driven filesystem
  access is resolved (symlinks followed) and prefix-checked server-side.
  Unset means unrestricted — the desktop default. HTTP error responses
  return fixed messages instead of echoing exception text.
- **TalkPipe components**: the pipeline pieces are registered entry points
  usable in your own TalkPipe scripts — `vaultSearch`, `vaultChat`,
  `vaultTextSearch`, `searchLance`, `extractSearchKeywords`,
  `mergeSearchResults`, `filterSearchResults`, `buildVectorDBFromPaths`,
  and the `fileWatcher` source (plus the experimental `watchIntoVectorDB` /
  `listIntoVectorDB` watcher sources, which write a separate layout the
  interfaces do not read).
- **Vault format**: a vault is a single folder holding a LanceDB `docs`
  table (compatible with TalkPipe's `makevectordatabase --path`), an
  on-demand Whoosh full-text index (`fulltext_vault/`), and
  `vault_metadata.json`; it can be copied or moved as a unit.

### Upgrading from a 0.0.x release

- The default embedding model is now model2vec
  (`minishlab/potion-retrieval-32M`), computed in-process. Vaults indexed
  under the old `ollama`/`embeddinggemma` default predate
  `vault_metadata.json`, so their embedder cannot be restored
  automatically: re-index them (Overwrite) or switch the embedding settings
  back before searching. The Settings page's configuration status flags
  such legacy vaults.
- The legacy `vault_path/vector_vault` layout is rejected when opened, with
  migration guidance: LanceDB now lives directly at the vault path.
- `OLLAMA_BASE_URL` was documented in earlier releases but never read. The
  Ollama server URL comes from `TALKPIPE_OLLAMA_SERVER_URL` (or
  `OLLAMA_SERVER_URL` in `~/.talkpipe.toml`), or from the Settings page.
- Requires Python 3.11.4+ and `talkpipe[all]>=1.0.0b2`, the first TalkPipe
  release that ships typed decorators and a `py.typed` marker.
