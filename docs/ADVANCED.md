# TalkPipe Vault — Advanced Usage

Reference material beyond [getting the web app running](../README.md):
command-line tools, the terminal interface, containers, the model
configuration reference, agent search, retrieval filters, and where indexes
live on disk. To use the vault's components in pipelines of your own, see
the [Developer Guide](DEVELOPER_GUIDE.md).

## Command-line tools

The package installs three console scripts: `vault-index` (index folders
from the command line), `vault-server` (the web application) and
`vault-tui` (the terminal interface). Named vaults are built with TalkPipe's
`makevectordatabase`, installed with the TalkPipe dependency.

### `vault-index`

```bash
vault-index FOLDER [FOLDER ...] [--embedding-source S --embedding-model M] [--keywords-only] [--index-location auto|home] [--json] [-v]
```

Indexes each folder the way `vault-server` does in the background, and
exits: new and changed files are indexed, deleted ones removed, keywords
first and then vectors. The index is the folder's `.tpvault` (or its place
in app data), so `vault-server FOLDER` and `vault-tui FOLDER` open it as-is,
and running the command again processes only what changed. It prints one
line per folder with the file counts and the embedder, and exits non-zero
when a file failed, the embedder was unreachable, or another vault process
was already indexing the folder (which it then leaves alone).

- The embedder, chunk settings and index location saved on the Settings
  page are the defaults; `--embedding-source` / `--embedding-model` override
  them for a folder with no vectors yet. A folder already indexed keeps the
  embedder its vectors were made with.
- `--keywords-only` builds just the keyword index, with no embedder.
- `--json` prints each folder's full status dict as a JSON line.
- `-v` shows the indexing log and talkpipe's per-file progress, which are
  otherwise hidden.

It runs the `indexFolder` segment as a ChatterLang script; the
[Developer Guide](DEVELOPER_GUIDE.md#index-a-folder-once-indexfolder) shows
the same thing inside a pipeline.

### `makevectordatabase`

Builds the LanceDB `docs` table that `vault-server` reads — the same layout
the **Vaults & Documents** page produces. Embedding configuration is required:
pass it explicitly or set it in the TalkPipe config.

```bash
makevectordatabase "/path/to/documents/**/*.txt" \
    --path ~/my-vault \
    --embedding_source model2vec \
    --embedding_model minishlab/potion-retrieval-32M \
    --overwrite
```

Note: vaults built with `makevectordatabase` alone lack the
`vault_metadata.json` embedding record (see
[Vault storage structure](#vault-storage-structure)) and are treated as
legacy vaults — the Settings page flags them when the current embedder may
not match the index.

### `vault-server`

```bash
vault-server [~/notes | ~/my-vault] [--dir FOLDER ...] [--as-vault] [--resume] [--host 0.0.0.0] [--port 8002] [--show-source-paths] [--no-browser]
```

- The path is optional; without it the app reopens the folders the last
  session had open, or starts on the Vaults & Documents page when the last
  session had none (it used a named vault, or every folder was removed).
- A folder of documents opens in **folder mode** (see
  [Folder mode](#folder-mode)): it is searched directly, indexed in the
  background into `~/notes/.tpvault`. A vault folder, or an empty or
  missing one, opens as a named vault as before (a missing one is created,
  and the startup banner says it is new and empty, so a mistyped folder
  name shows); `--as-vault` forces that for a folder of documents (the
  behavior before folder mode).
- `--dir FOLDER` adds a folder to search; repeat it for several. One that
  does not exist is left out with a warning.
- `--resume` reopens what the web interface last had open: its folders, or
  else the vault most recently used, falling back to the given path (or the
  Vaults & Documents page) when nothing is usable. This is how the container
  image starts.
- `--show-source-paths` shows source file paths in search results and serves
  those files over HTTP; hidden by default. Opening documents via the Open
  link on results and citations does not need this flag — it identifies the
  file by its index entry without exposing the path.
- `--no-browser` skips opening a browser on startup (headless servers,
  services, containers).

The app module can also be run directly with the same options:

```bash
python -m talkpipe_vault.apps.query ~/my-vault --host 0.0.0.0 --port 8002
```

### `vault-tui`

```bash
vault-tui [~/notes | ~/my-vault] [--dir FOLDER ...] [--as-vault] [--resume] [--show-source-paths]
```

The terminal interface described in the [README](../README.md#the-terminal-interface-vault-tui).
The path, `--dir`, `--as-vault`, `--resume` and `--show-source-paths` mean
the same as for `vault-server`; there is no host/port because it runs in-process. It reads
and writes the same files as the web interface (recent vaults, settings and
credentials under `TALKPIPE_VAULT_HOME`, default `~/.talkpipe-vault`) and
honours the path fences below. Press `F1` inside the app for the keyboard
reference. The module form is `python -m talkpipe_vault.tui`.

### Terminal interface notes

- **Opening.** A folder of documents opens in folder mode; `--as-vault`
  opens it as a named vault. Opening a named vault loads the embedding model
  (a first run downloads it, about 250 MB); the header reads "opening…"
  until it is ready. `Enter` in either path field on the Vault tab runs
  **Index documents** (it opens the vault when only the vault path is
  filled).
- **Settings tab.** Choose the chat provider under **Model settings**, then
  enter its key or server URL under **Connections & credentials**, the last
  section (one `Shift+Tab` from the Re-test button jumps to its last field;
  `PageUp`/`PageDown` scroll). **Save connection settings** re-tests the
  configuration. A bare `host:11434` becomes `http://host:11434`. A saved
  value beats the environment variable (`TALKPIPE_OLLAMA_SERVER_URL`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`); clear the field, or tick **Clear
  the saved key**, to fall back to it. The OpenAI base URL field points the
  OpenAI provider at any compatible endpoint ([Provider notes](#provider-notes)).
- **Ask.** `Esc` stops waiting for an answer. The question box grows as it
  wraps; `PageUp`/`PageDown` in it scroll a long answer. **Copy answer**
  copies the model's text.
- **Search results.** The result list takes the focus after a search;
  `F3`/`F4` or `Shift+Tab` return to the query field. Long chunk text scrolls
  once you `Tab` into its pane.
- **Named vaults.** **Index documents** adds to the open vault; tick
  **Overwrite existing index** to replace it (re-indexing the same folder
  without it duplicates every chunk, and the summary says so). Indexing a
  named vault never updates its full-text index: the header shows "keywords
  out of date" until you rebuild it on the Keywords tab. Folders keep both
  their indexes current on their own.
- **Quitting.** `Ctrl+Q` quits (`Ctrl+C` only reminds you); while an indexing
  run is in progress it asks first. `Ctrl+R` reloads the vault and settings
  after editing `~/.talkpipe.toml` outside the app. If the open vault's
  folder disappears from disk, Search, Ask and `Ctrl+R` say so instead of
  recreating it empty.
- **Retrieval filter.** The button on the Vault tab edits the same per-vault
  ChatterLang script as the web page (its **Help** button shows the result
  shape and recipes); a saved filter does nothing until **Enabled on this
  machine** is ticked. Syntax: [Writing a retrieval filter](#writing-a-retrieval-filter).

### Confining paths on a shared machine

Two environment variables restrict where either interface may look. Both
are unset by default (no restriction), apply to `vault-server` and
`vault-tui` alike, and are checked against fully resolved paths, so a
symlink pointing outside an allowed root is rejected. If either names a
folder that does not exist, `vault-server` refuses to start and says which
one; `vault-tui` starts on the Vault tab with the same message on screen
and no vault open, so the variable can be fixed and the app restarted.

- `TALKPIPE_VAULT_ROOT` — a single directory. Vaults can only be created,
  opened, deleted, or resumed inside it, and the vault name suggested for a
  new documents folder is placed under it instead of in your home directory
  (`/mnt/archive/notes` → `$TALKPIPE_VAULT_ROOT/notes-vault`, rather than the
  unrestricted default `~/notes-vault`). The suggestion is only a pre-filled
  value in either case — edit it or browse elsewhere before submitting.
- `TALKPIPE_DOCUMENT_ROOTS` — one or more directories separated by the
  platform path separator (`:` on Linux/macOS, `;` on Windows). The folder
  picker and document indexing are confined to them.

```bash
export TALKPIPE_VAULT_HOME=/srv/vault/home          # settings, credentials, recent list
export TALKPIPE_VAULT_ROOT=/srv/vault/vaults
export TALKPIPE_DOCUMENT_ROOTS=/srv/documents:/srv/archive
vault-tui --resume
```

## Containers

The [README](../README.md#option-2-container-podman-or-docker) covers the
basic `podman run` invocation. Additional options:

### macOS and Windows

Containers run inside the podman machine VM (Podman Desktop sets this up).
In PowerShell, replace the `\` line continuations with backticks and write
the documents path explicitly (`-v C:\Users\you\Documents:/documents:ro`).
Before indexing a large collection, give the VM more memory than its default
(often 2 GB): a big ingestion peaks around 1.5 to 2 GB and an over-limit kill
is silent (exit code 137, `oom=true` in `podman inspect`).

```bash
podman machine stop
podman machine set --memory 4096    # MiB; use 8192 for very large collections
podman machine start
```

Chat providers under a container: `TALKPIPE_OLLAMA_SERVER_URL` is only for
Ollama on the host. Environment variables work for the others too, for
example `-e TALKPIPE_CHAT_SOURCE=anthropic`, `-e TALKPIPE_CHAT_MODEL=<model>`
and `-e ANTHROPIC_API_KEY=…`, as does entering the key on the Settings page,
which is saved in the data volume.

### Compose

`docker-compose.yml` provides the same setup as a compose service,
configured through `.env` (see `.env.example`):

```bash
cp .env.example .env
# Edit .env and set VAULT_DOCUMENTS_DIR to the absolute path of the folder
# you want to index; the services refuse to start without it, because
# compose does not expand "~" in a volume mapping.

pip install podman-compose   # podman needs a compose provider
podman compose up -d
```

Nothing to search is configured here: the service starts
`vault-server --resume`, so it reopens the folders (or vault) you last had
open and otherwise opens on the Vaults & Documents page. Folder indexes and
vaults live under `/app/data`, the persistent volume, since `/documents` is
mounted read-only.

Any provider works under compose. Choose the source and enter its API key on
the Settings page — both persist in the data volume, so they survive
container recreation and need nothing in the environment. To configure a
provider from the environment instead, add the variables to the service's
`environment:` list (for example `- ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}`)
or point the service at an `env_file`: compose passes the container only the
variables that service names, so a key left in `.env` alone does not reach
it. The shipped service names `TALKPIPE_OLLAMA_SERVER_URL` (defaulting to an
Ollama on the container host, `http://host.containers.internal:11434`)
because that is the default chat source's one setting.

### Deriving a customized image

To ship different default models and extra Python packages, build a small
image on top of this one — the entrypoint, port, volumes, and path fences
are all inherited:

```dockerfile
# Containerfile.custom
FROM ghcr.io/sandialabs/talkpipe-vault:latest

# Extra Python packages (e.g. a TalkPipe plugin or provider SDK). The base
# image runs as user "vault", so switch to root to install system-wide.
USER root
RUN pip install --no-cache-dir your-extra-package
USER vault

# Different default sources/models for embeddings and chat — any registered
# source works (model2vec, ollama, openai, anthropic, ...). TalkPipe reads
# these at the configuration level, so they replace the built-in defaults
# (model2vec / ollama+mistral-small) but choices saved on the web Settings
# page still take precedence.
ENV TALKPIPE_EMBEDDING_SOURCE=ollama
ENV TALKPIPE_EMBEDDING_MODEL=nomic-embed-text
ENV TALKPIPE_CHAT_SOURCE=ollama
ENV TALKPIPE_CHAT_MODEL=llama3.1
```

Build and run it exactly like the base image:

```bash
podman build -t my-talkpipe-vault -f Containerfile.custom .
podman run --rm -p 8002:8002 -v vault_data:/app/data \
  -v ~/Documents:/documents:ro \
  -e TALKPIPE_OLLAMA_SERVER_URL=http://host.containers.internal:11434 \
  my-talkpipe-vault
```

Keep in mind:

- Ollama-backed sources still need `TALKPIPE_OLLAMA_SERVER_URL` at run time;
  OpenAI and Anthropic sources need their API key (`-e OPENAI_API_KEY=…` /
  `-e ANTHROPIC_API_KEY=…`, or entered on the Settings page). Don't bake keys
  into the image.
- The embedding default applies to vaults indexed from now on; an existing
  vault reopens with the embedder recorded in its `vault_metadata.json`
  regardless of the new default.

## Model configuration

Configuration precedence, highest to lowest:

1. **Explicit parameters** in code/CLI
2. **Web interface Settings page** (persisted under
   `~/.talkpipe-vault/settings.json`; relocate with `TALKPIPE_VAULT_HOME`)
3. **TalkPipe configuration** — `~/.talkpipe.toml` or `TALKPIPE_*`
   environment variables
4. **Defaults** in `pipelines/config.py`

API keys and connection URLs entered under **Settings → Connections &
credentials** are persisted separately to `~/.talkpipe-vault/credentials.json`
(owner-only permissions) and applied to the vault process only — your shell
environment and `~/.talkpipe.toml` are left untouched.

### Configuration methods

Via `~/.talkpipe.toml` (keys work in a `[vault]` section or at top level):

```toml
[vault]
embedding_model = "text-embedding-3-large"
embedding_source = "openai"
chat_model = "gpt-4o-mini"
chat_source = "openai"
```

Or via environment variables:

```bash
export TALKPIPE_EMBEDDING_MODEL="text-embedding-3-large"
export TALKPIPE_EMBEDDING_SOURCE="openai"
export TALKPIPE_CHAT_MODEL="gpt-4o-mini"
export TALKPIPE_CHAT_SOURCE="openai"
export OPENAI_API_KEY="sk-your-key-here"
```

### Supported configuration keys

| Key | Alternative keys | Description | Default |
|-----|------------------|-------------|---------|
| `embedding_model` | `EMBEDDING_MODEL`, `default_embedding_model_name` | Model name for generating embeddings | `minishlab/potion-retrieval-32M` |
| `embedding_source` | `EMBEDDING_SOURCE`, `default_embedding_model_source` | Provider for embedding model (`model2vec`, `ollama`, `openai`) | `model2vec` |
| `chat_model` | `CHAT_MODEL`, `default_model_name` | Model name for chat/completion | `mistral-small` |
| `chat_source` | `CHAT_SOURCE`, `default_model_source` | Provider for chat model (`ollama`, `openai`, `anthropic`, `eliza`) | `ollama` |
| `document_template` | `DOCUMENT_TEMPLATE` | Formats full documents before embedding. Placeholders: `{title}`, `{content}` | `"title: {title} \| text: {content}"` |
| `shingle_template` | `SHINGLE_TEMPLATE` | Formats shingled chunks before embedding. Placeholders: `{title}`, `{shingle}` | `"title: {title} \| text: {shingle}"` |
| `retrieval_template` | `RETRIEVAL_TEMPLATE` | Formats search queries before embedding. Placeholder: `{query}`. Set it when your embedder was trained with a query prefix (e.g. `"search_query: {query}"`) | `"{query}"`; for EmbeddingGemma models `"task: search result \| query: {query}"` |

### Provider notes

Vault can use any embedding or chat provider registered with TalkPipe; the
README's [LLM providers](../README.md#llm-providers) section has the
overview and TalkPipe's
[model and source configuration guide](https://github.com/sandialabs/talkpipe/blob/stable/docs/guides/model-and-source-configuration.md)
the library-wide reference. Only the providers you select need setting up.
OpenAI and Anthropic keys are read from the environment (or the Settings
page), not from `~/.talkpipe.toml`.

- **model2vec** (default, embeddings only): runs fully in-process — no
  server, no API key. The model downloads from Hugging Face on first use and
  is cached; use TalkPipe's `talkpipe_precache_model2vec` to prefetch for
  offline machines.
- **Ollama** (embeddings and chat; the default chat provider): set the
  server URL in **Settings → Connections & credentials**, via
  `TALKPIPE_OLLAMA_SERVER_URL`, or with `OLLAMA_SERVER_URL` in
  `~/.talkpipe.toml` (default `http://localhost:11434`). The model must
  already be pulled on that server.
- **OpenAI** (embeddings and chat): set `OPENAI_API_KEY` in the environment
  or enter it in the Settings page. A custom base URL — the **OpenAI base
  URL** field under **Settings → Connections & credentials**, or
  `OPENAI_BASE_URL` in the environment — points this provider at any
  OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp server, a proxy).
- **Anthropic** (chat only): model name such as `claude-sonnet-4-5`; key via
  Settings page or `ANTHROPIC_API_KEY`.
- **eliza** (chat only, built-in): a scripted responder, not a language
  model — it needs no server or key and its replies do not use the
  retrieved context. Use it only to smoke-test the Ask page before a real
  chat provider is configured, never to judge answers.
- **Plugins**: the Settings dropdowns are populated from TalkPipe's provider
  registry, so a TalkPipe plugin that registers an additional embedding or
  chat provider appears there automatically once installed. See
  [Deriving a customized image](#deriving-a-customized-image) for installing
  plugin packages in the container.

## Agent search

**Let an agent select the results** (Semantic Search) and **Use Agent**
(Ask) hand retrieval to an agent built on talkpipe-agents' `llmAgent`. It
runs semantic and keyword searches as tools, reads what comes back, keeps the
relevant chunks and discards the rest, and rewrites its queries from what it
learns. **It stops when it judges that what it has kept would answer the
question well**, after one chunk or after eight; the Settings page's chunk
limit and iteration cap only bound a run that never gets there. The results
note says which happened, counts the chunks it saw but never kept or ignored
as "undecided", and every tool call is printed to the server console and
listed under the results.

- It runs on the chat model chosen under Settings and needs one that can
  call tools. Whether a model can only shows when a run starts: a server
  without tool calling ends the run with an error note quoting what it said.
- The default `mistral-small` often stops without keeping what it found (the
  note then says it "stopped without saying it had enough"). In testing,
  `qwen3:8b` and `gpt-oss` ran the agent cleanly; `minimax-m3.1-flash:cloud`
  usually used up its iterations searching and reading without deciding
  (the [developer's guide](AGENT_RETRIEVAL.md) says why).
- On Ask, **Use Agent** supersedes the keyword boost (keyword search is one
  of the agent's tools), and the answer is built from the chunks it kept,
  which are also the citations. **Extra Effort** doubles the iterations (16
  instead of 8 with the default); it helps models strong at tool calling and
  mostly slows weaker ones. When the provider cannot run an agent at all
  (`eliza`), the pages say so in place of the checkbox.
- The keyword boost without the agent: the chat model distills the question
  into index keywords, the matches are merged with the vector results, and
  the "Answered by" line reports how many keyword hits were merged
  (duplicates of vector hits are combined, so the boost can be active when
  the source list looks unchanged).

To use the agent's tools in your own application, or over your own search
backend, see the [agent retrieval developer's guide](AGENT_RETRIEVAL.md).

## Retrieval filters

A retrieval filter is a per-index ChatterLang script that runs over each
retrieved result before Ask builds its prompt. It is edited on the Vaults &
Documents page (or the Vault tab), for the open named vault or the one open
folder, and applies on this machine only once **Enabled on this machine** is
ticked. The same mechanism is a segment, `filterSearchResults`, for
pipelines of your own ([Developer Guide](DEVELOPER_GUIDE.md#filtering-results-filtersearchresults)).

### Writing a retrieval filter

The retrieval filter (**Vaults & Documents → Retrieval filter**, shown for
the open named vault or the one open folder) is a segment-only ChatterLang
pipeline — no `INPUT FROM` source, loops, or forks — that sees one retrieved
result at a time as a dict:

```
{"doc_id": "…", "score": 0.87,
 "document": {"content": "…", "source": "…", "title": "…"}}
```

Emit a result to keep it, drop it to filter it, modify it to transform it.
`lambdaFilter` handles arbitrary tests, with the result bound to `item`:

```
| lambdaFilter[expression="'draft' not in item['document'].get('content', '').lower()"]
```

For plain containment — *is this text inside that field?* — `isIn` and
`isNotIn` express the same filter without an expression, taking the field as
a dotted path:

```
| isNotIn[field="document.content", value="draft"]
| isIn[field="document.source", value="/notes/"]
```

Both segments also chain, so several containment rules read as a pipeline:

```
| isNotIn[field="document.content", value="draft"] | isIn[field="document.source", value="/notes/"]
```

They match case-sensitively and require the named field to be present on
every result — a missing field raises, which leaves retrieval unfiltered
(or fails the question when the filter is marked strict). Use
`lambdaFilter` when a field may be absent, when case should be ignored, or
when the test is more than containment.

## Vault storage structure

**Stable layout** (what `vault-server` reads) — produced by the **Vaults &
Documents** page and by `makevectordatabase`:

- `docs` — LanceDB table of chunk embeddings
- `fulltext_vault/` — Whoosh full-text index, created on demand from the
  **Keyword Search** page
- `vault_metadata.json` — records the embedding source/model (and vector
  dimension) the vault was indexed with, so reopening the vault
  automatically uses a matching embedder; it travels with the vault if the
  folder is copied or moved. Written by the **Vaults & Documents** page; vaults
  built with `makevectordatabase` alone don't have it and are treated as
  legacy vaults.

**Experimental watcher layout** — produced by the in-development pipelines
in `pipelines/building_and_watching.py` (`listIntoVectorDB` /
`watchIntoVectorDB`). These tables are **not** read by `vault-server`:

- `full_documents` — embeddings for templated full-document content
- `shingled_chunks` — embeddings for overlapping chunk windows with
  composite ids like `first-last-source`
- `fulltext_vault/` — Whoosh index over full document content

### Folder mode

A folder searched directly keeps its index, an ordinary vault in the stable
layout above, in a hidden `.tpvault` subfolder of the folder:

```
<folder>/.tpvault/
  docs.lance/  fulltext_vault/  vault_metadata.json   # the stable layout
  manifest.db    # per-file state: stat, content hash, chunk count, phase
  status.json    # the indexing process's progress and heartbeat
  writer.lock    # which process indexes the folder
  .gitignore     # "*": git ignores the whole directory
  CACHEDIR.TAG   # borg, restic --exclude-caches and tar --exclude-caches skip it
```

The index goes under the app's data directory instead
(`$TALKPIPE_VAULT_HOME/folder-indexes/<name>-<hash>/`, or under
`TALKPIPE_VAULT_ROOT` when one is set) when the folder is:

- read-only;
- on a network share (nfs, cifs/smb, sshfs, …);
- inside a sync client's folder (Dropbox, OneDrive, Google Drive, iCloud,
  Syncthing);
- or when Settings says so.

An index already in place is always reused, wherever it is.

**Indexing**, in one background thread per process:

- The keyword phase reads and chunks every file once into the Whoosh index,
  so a folder is searchable by keyword within minutes.
- The vector phase then embeds those stored chunks into LanceDB.
- Changes are noticed by a file watcher, by a full scan when the folder is
  opened, and by a rescan every 30 minutes (10 where file events are
  unreliable).
- A file whose size or modification time changed is hashed, so a touched but
  unchanged file is not re-embedded. A changed or deleted file's chunks leave
  both indexes at once.
- Chunk ids are derived from the index and the file's path relative to the
  folder, so redoing a batch after a crash replaces rather than duplicates,
  and a moved folder keeps its index.

**Several processes** may open one folder:

- The one holding `writer.lock` (an OS lock, released when the process
  exits however it exits) indexes it.
- The others search it and show its progress, and one of them takes over
  when the writer exits.
- A machine-wide `ingest.lock` in the app's data directory lets only one
  batch run at a time across processes, which bounds memory.

**Searching several folders** fuses their results:

- Semantic hits from folders indexed with the same embedder merge by
  distance; other lists merge by reciprocal rank. Every hit names its folder.
- While a folder is still embedding, keyword matches for its unembedded
  files are included and the pages say the index is partial.
- The embedder a folder was indexed with is recorded in its
  `vault_metadata.json` and used for its queries. When Settings names
  another, the folder offers **Re-embed** (keyword search keeps answering
  meanwhile); when the chunk settings differ, **Rebuild**.

Folder and named-vault modes are exclusive in the interfaces; a named vault
whose documents all lie in one folder offers to search that folder instead.
Retrieval filters apply when one folder is open, not several.

A pipeline following a folder with `folderChanges` ([Developer Guide](DEVELOPER_GUIDE.md#following-changes-folderchanges)) keeps its record of what
it has sent in another hidden subfolder, `<folder>/.tpstream/<name>.db`
(with a `.gitignore` but no `CACHEDIR.TAG`, since a restore should bring it
back with the documents), or under `folder-streams/` in app data where an
index would go there. It is separate from `.tpvault`, so rebuilding or
deleting a folder's index never makes a stream send the whole folder again.
Delete a `<name>.db` to have that stream start over.

## Developing

Contributing to the vault, the project layout, and the test and lint
commands are in the [Developer Guide](DEVELOPER_GUIDE.md#contributing-to-the-vault).
