<p align="center">
  <img src="docs/talkpipe_vault.jpg" alt="TalkPipe Vault Logo" width="300">
</p>

# TalkPipe Vault

> Turn folders of documents into a searchable, question-answerable vault — on your own machine.

[![Python 3.11.4+](https://img.shields.io/badge/python-3.11.4+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Development Status](https://img.shields.io/badge/status-alpha-yellow.svg)](https://github.com/sandialabs/talkpipe-vault)

<p align="center">
<img src="docs/vault-homepage.png" alt="Talkpipe Homepage" width="100%">
</p>
<p align="center">
<img src="docs/vault-search.png" alt="vault search example" width="100%">
</p>

## What is TalkPipe Vault?

TalkPipe Vault is a web application that indexes your documents — notes,
papers, reports, an Obsidian vault, a project archive — into a local
[LanceDB](https://lancedb.com/) vector database and lets you explore them
three ways:

- **Semantic search** — find documents by meaning, not just words
- **Keyword search** — precise full-text queries with boolean operators
- **Ask** — single-turn Q&A with answers grounded in your documents

Everything runs locally by default. The built-in embedding model (model2vec)
runs in-process with no server or API key; generated answers can come from
any LLM provider TalkPipe supports — a local [Ollama](https://ollama.com/)
server, OpenAI, or Anthropic — and TalkPipe plugins can add others. Ollama
is one option, not a requirement: see [LLM providers](#llm-providers). Your
documents are only ever sent to the provider you choose.

It is built on the [TalkPipe](https://github.com/sandialabs/talkpipe)
pipeline framework and doubles as a real-world example of composing document
processing, vector search, and RAG from reusable components — see the
[Advanced Guide](docs/ADVANCED.md) if that side interests you.

**Status:** alpha, under active development. This page is the `master` branch,
which runs ahead of the PyPI release — if something described here is missing
from a `pip install`, install the development branch to get the documented
behavior:

```bash
pip install "git+https://github.com/sandialabs/talkpipe-vault.git@master"
```

The `@master` matters: the repository's default branch is the release-only
`stable`, so a plain `git+https://…` URL installs the same code as PyPI. See
[Development setup](docs/ADVANCED.md#development-setup) for a full clone.

## Run the web app

### Option 1: pip install

```bash
# A virtual environment avoids PEP 668 "externally managed" errors on
# recent Linux distributions.
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install talkpipe-vault
vault-server
```

Your browser opens at http://127.0.0.1:8002 (`--no-browser` skips that). Then:

1. **Vaults & Documents** — pick the folder (or glob pattern) to index. A
   vault name is suggested for you (under your home directory); one click
   creates the vault and indexes into it. The first index downloads the
   default embedding model from Hugging Face (about 250 MB on disk, cached
   afterward).
2. **Semantic Search** and **Ask** away.
3. **Keyword Search** works once you build its index: that page starts
   disabled and offers a **Create Full-Text Index** button, which builds a
   separate full-text index from the documents already in the vault. It is a
   one-off, but indexing more documents later does not refresh it — rebuild
   it from the same page when you do.

Search and indexing need nothing more. Answers on the Ask page need a chat
provider — Ollama, OpenAI, or Anthropic. Pick it on the **Settings** page
and enter its server URL or API key under **Connections & credentials**
there (no environment variables needed); [LLM providers](#llm-providers)
has the details. The chat setting starts out as Ollama at
`http://localhost:11434`, so until a reachable provider is configured, Ask
shows a connection error that explains how to fix it.

### Option 2: Container (Podman or Docker)

```bash
podman run --rm -p 8002:8002 \
  -v vault_data:/app/data \
  -v ~/Documents:/documents:ro,Z \
  -e TALKPIPE_OLLAMA_SERVER_URL=http://host.containers.internal:11434 \
  ghcr.io/sandialabs/talkpipe-vault:latest
```

Then open http://127.0.0.1:8002 (use `127.0.0.1`, not `localhost` — rootless
podman publishes ports IPv4-only). Docker users can substitute `docker run`
with the same arguments.

What each piece does:

- `-v vault_data:/app/data` — persistent storage for vaults, settings, and
  the embedding-model cache, so the model downloads once and your data
  survives container recreation. On start the container reopens the vault
  you last used; the first run starts on the Vaults & Documents page.
- `-v ~/Documents:/documents:ro,Z` — host documents to index; the folder
  picker only sees what you mount. Mount `~` instead to browse your whole
  home directory. Keep `:Z` on SELinux Linux hosts (e.g. Fedora); **drop it
  on macOS and Windows**, where it makes podman try to relabel every mounted
  file.
- `-e TALKPIPE_OLLAMA_SERVER_URL=…` — **optional**, and only for Ollama (the
  default chat setting) running on the container host. Using OpenAI or
  Anthropic instead? Drop the line, select the provider on the Settings page
  and enter its API key under **Connections & credentials** — both are saved
  in the data volume. Environment variables work too — for example
  `-e TALKPIPE_CHAT_SOURCE=anthropic`, `-e TALKPIPE_CHAT_MODEL=<model>` and
  `-e ANTHROPIC_API_KEY=…`; see [LLM providers](#llm-providers). Search and
  indexing work without any chat provider.

**macOS/Windows notes:** containers run inside the podman machine VM
(Podman Desktop sets this up). In PowerShell, replace the `\` line
continuations with backticks and write the documents path explicitly
(`-v C:\Users\you\Documents:/documents:ro`). Before indexing a large
collection, give the VM more memory than its default (often 2 GB) — a big
ingestion peaks around 1.5–2 GB and an over-limit kill is silent
(exit code 137, `oom=true` in `podman inspect`):

```bash
podman machine stop
podman machine set --memory 4096    # MiB; use 8192 for very large collections
podman machine start
```

A compose service and instructions for deriving your own customized image
(different default models, extra packages) are in the
[Advanced Guide](docs/ADVANCED.md#containers).

## The terminal interface (`vault-tui`)

Everything above is also available without a browser — in an SSH session, a
tmux window, or on a headless machine — through `vault-tui`. It is installed
by the same `pip install talkpipe-vault` (or container image) as
`vault-server`; there is nothing extra to install.

```bash
vault-tui ~/my-vault      # open (or create) a vault — the index folder, not your documents
vault-tui --resume        # reopen the most recently used vault
vault-tui                 # start on the Vault tab and choose one there
```

A vault is a folder that holds the search index; the documents live wherever
they already are and are named on the Vault tab. Point `vault-tui` at a folder
of documents by mistake and it asks before turning that folder into a vault.
Opening a vault loads the embedding model, which on a first run means
downloading it (about 250 MB for the default model2vec model) — the Vault tab
says so while it waits, and the header reads "opening…" until the vault is
ready.

Enter in either path field on the Vault tab runs **Index documents** (it
opens the vault when only the vault path is filled).

It runs in-process (no server needed) and uses the same vault files, recent
list, model settings and credentials as the web interface, so you can switch
between the two freely. Tabs mirror the web pages:

| Key | Tab | What you can do |
|-----|-----|-----------------|
| `F2` | Vault | Open/create a vault, browse to a documents folder, index it (with progress), open or delete recent vaults, edit the retrieval filter |
| `F3` | Search | Semantic search; the detail pane follows the highlighted result — `Enter` loads the full chunk, `o` shows/opens the source document, `c` copies the chunk, Copy All copies every result |
| `F4` | Keywords | Full-text search (Whoosh syntax), and building/rebuilding the full-text index |
| `F5` | Ask | Question answering with the answer, its "Answered by" line and the source chunks it used; optional keyword boost |
| `F6` | Settings | Configuration status (Re-test), embedding/chat model settings, connections & credentials |
| `F1` / `Ctrl+R` / `Ctrl+Q` | | Help / reload the vault and settings (after indexing or editing `~/.talkpipe.toml` outside the app) / quit (`Ctrl+C` only reminds you of `Ctrl+Q`; while an indexing run is in progress `Ctrl+Q` asks first, because quitting abandons it) |

`--show-source-paths` shows file paths in results, as for `vault-server`.
On a shared machine, `TALKPIPE_VAULT_ROOT` and `TALKPIPE_DOCUMENT_ROOTS`
confine where vaults and documents may live for both interfaces — see
[Confining paths on a shared machine](docs/ADVANCED.md#confining-paths-on-a-shared-machine).
Long operations (embedding, Ask, indexing) run in the background and report
progress in the tab that started them. As in the browser, Ask needs a chat
provider (Ollama, OpenAI, or Anthropic — see [LLM providers](#llm-providers)):
choose it under **Model settings** on the Settings tab (`F6`), then enter
its API key or server URL under **Connections & credentials** — the last
section on the tab (one `Shift+Tab` from the top, the Re-test button `F6`
lands on, jumps straight to its last field, the Ollama URL;
`PageUp`/`PageDown` scroll the tab) — and press **Save connection
settings**, which re-tests the configuration by itself. A bare
`host:11434` is completed to `http://host:11434` when saved. Alternatively
export the provider's variable (`TALKPIPE_OLLAMA_SERVER_URL`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) before starting — but a value saved
on the Settings tab takes precedence over the variable, so clear the URL
field (or tick **Clear the saved key**) to go back to it; the configuration
status names which one is in effect.
The OpenAI base URL field points the OpenAI provider at any
OpenAI-compatible endpoint — see
[Provider notes](docs/ADVANCED.md#provider-notes). While an
answer is being generated, `Esc` stops waiting for it. "Index
documents" adds to the open vault — tick **Overwrite existing index** to
replace it; re-indexing the same folder without it duplicates every chunk
(the summary line says so when it happens, and the box unticks itself after
a replace run). Indexing never updates the full-text index: the header
shows "keywords out of date" until you rebuild it on the Keywords tab, and
a keyword search that finds nothing says so. Long chunk text scrolls once
you `Tab` into its pane; the question box grows as a long question wraps,
and `PageUp`/`PageDown` in it scroll a long answer (the answer pane is also
four `Tab` stops from the question box). After a search the result list has
the focus, so `F3`/`F4` (or `Shift+Tab`) return to the query field before
you type the next query. If the open vault's folder disappears from disk
(deleted or unmounted outside the app), Search, Ask and `Ctrl+R` say so
instead of quietly recreating it empty. The **Retrieval filter** button on the Vault tab edits the same
per-vault ChatterLang script as the web page, with an example in the
dialog (its **Help** button adds the result shape and more recipes); a
saved filter does nothing until you tick **Enabled on this machine**. The
script syntax is in the Advanced Guide under
[Writing a retrieval filter](docs/ADVANCED.md#writing-a-retrieval-filter).

## The web interface

- **Vaults & Documents** — choose the documents to index and the vault to
  index them into, on one page with a built-in folder browser. With no vault
  open, a vault name is suggested from the documents folder and created on
  submit; with one open, the same form adds documents to it. Recent vaults
  are remembered for one-click reopening, and indexing shows live progress.
- **Settings** — choose embedding and chat providers/models, with a live
  **Configuration status** panel that tests your selection (and can download
  an uncached embedding model via Re-test), plus **Connections &
  credentials** for the OpenAI and Anthropic API keys and the Ollama and
  OpenAI-compatible server URLs — no environment variables required. See
  [LLM providers](#llm-providers).
- **Semantic Search** — vector similarity search over your documents. The
  closest ten chunks are always returned, so on a small vault every chunk
  comes back and the last few are weak matches.
- **Keyword Search** — boolean and phrase queries, over a separate full-text
  index you build once with the button on that page (the page says so, and
  stays disabled until you do). Indexing documents never updates it, so
  rebuild it there after adding more. Matching is case-insensitive but on
  exact word tokens (`apple` won't match `apples`), and very common words are
  not indexed; use semantic search for meaning-based lookups.
- **Ask** — single-turn Q&A with source citations you can open and copy.
  Once a full-text index exists, a **Boost retrieval with keyword search**
  checkbox appears: the chat model distills your question into index
  keywords, the keyword matches are merged with the vector-search results,
  and the combined context is used to answer. The "Answered by" line under
  the answer confirms whether the boost ran and how many keyword hits were
  merged (keyword hits that duplicate vector results are combined, so the
  boost can be active even when the source list looks unchanged).

Every search result and Ask citation has an **Open** link that fetches the
original document from the server — PDFs, images, and plain text open right
in the browser; other formats download. Because the file is streamed over
HTTP, this works the same when the server runs in a container (where your
documents live at a container-side mount path the browser can't reach
directly).

## LLM providers

TalkPipe Vault is not tied to Ollama. It uses two models — one for
**embeddings** (indexing and semantic search) and one for **chat** (Ask
answers) — and each can come from any provider registered with TalkPipe
that supports that role. Out of the box:

| Provider | Embeddings | Chat | What it needs |
|----------|------------|------|---------------|
| `model2vec` | yes — **default** (`minishlab/potion-retrieval-32M`) | — | Nothing: runs in-process; the model downloads from Hugging Face once |
| `ollama` | yes | yes — **default** (`mistral-small`) | A reachable Ollama server (default `http://localhost:11434`) with the model pulled |
| `openai` | yes | yes | An API key; optionally a base URL for any OpenAI-compatible endpoint |
| `anthropic` | — | yes | An API key |
| `eliza` | — | plumbing check only | Nothing — see below |

Any provider a TalkPipe plugin registers appears alongside these
automatically; TalkPipe's
[supported sources](https://github.com/sandialabs/talkpipe/blob/stable/docs/guides/model-and-source-configuration.md#supported-sources)
has the library's full list.

**Choosing.** Pick the source and model for each role on the **Settings**
page (the Settings tab, `F6`, in `vault-tui`); choices persist and apply
immediately, and the **Configuration status** panel tests whatever is
selected. Only the providers you select need to be set up — the default
combination needs just an Ollama server, and search and indexing need no
chat provider at all. `TALKPIPE_EMBEDDING_SOURCE`/`TALKPIPE_EMBEDDING_MODEL`
and `TALKPIPE_CHAT_SOURCE`/`TALKPIPE_CHAT_MODEL` (or `embedding_source`,
`chat_model`, … in `~/.talkpipe.toml`) replace the defaults above; a choice
saved on the Settings page overrides them.

**Credentials.** Enter keys and URLs under **Settings → Connections &
credentials** — no environment variables needed — or supply them through
the environment:

| Setting | Environment variable |
|---------|----------------------|
| OpenAI API key | `OPENAI_API_KEY` |
| OpenAI base URL (optional) | `OPENAI_BASE_URL` |
| Anthropic API key | `ANTHROPIC_API_KEY` |
| Ollama server URL | `TALKPIPE_OLLAMA_SERVER_URL` |

Values saved on the Settings page are stored in `credentials.json` under
`TALKPIPE_VAULT_HOME` (default `~/.talkpipe-vault`, owner-only permissions),
apply only to the vault process, and take precedence over the environment
variable; clear a field to fall back to the variable.

**eliza is not a model.** It is a built-in scripted responder whose
replies do not draw on your documents. Select it only to check that the Ask
page works end to end before a real provider is set up — its replies say
nothing about answer quality.

**The embedding model belongs to the vault.** Embeddings are only comparable
to queries embedded by the same model, so each vault records the embedder it
was built with and reopens with it, regardless of the current setting;
switching embedders means re-indexing. Chat models can be switched freely at
any time.

The full reference — precedence, every configuration key, templates, and
per-provider notes — is in the
[Advanced Guide](docs/ADVANCED.md#model-configuration).

## More documentation

The [Advanced Guide](docs/ADVANCED.md) covers:

- Command-line indexing with `makevectordatabase` and the full `vault-server`
  flag reference
- The compose service and deriving a customized container image
- The complete model configuration reference
- Architecture, the reusable TalkPipe sources/segments, and building your own
  pipelines
- Vault storage layout
- Development setup
- The experimental directory-monitoring components

## Requirements

- **Python** 3.11.4+ (pip install path)
- **A chat provider** for Ask answers — any one of a local Ollama server, an
  OpenAI API key (or OpenAI-compatible endpoint), or an Anthropic API key;
  see [LLM providers](#llm-providers). Nothing else is needed: embeddings
  work out of the box, and indexing and search need no chat provider.

## Contributing

Contributions are welcome. Before submitting: `pytest` passes, and
`ruff check .`, `ruff format --check .`, and `mypy` are clean -- CI fails on
any finding from those three (`ruff check --fix . && ruff format .` fixes
most; `pre-commit install` runs them on every commit). See
[Development setup](docs/ADVANCED.md#development-setup).

### Development environment

The default branch, `stable`, is release-only: it points at the latest
release, so what you see on the repository's front page describes that
release. Development happens on `master`, which is where merge requests go
and where unreleased changes and their documentation accumulate — check it
out first (`git checkout master` after cloning).

Local development uses [uv](https://docs.astral.sh/uv/) against the committed
`uv.lock`, so contributors share one reproducible set of versions:

```bash
uv sync --extra dev
uv run pytest
```

**CI does not use the lockfile.** It installs with pip (`pip install -e
'.[dev]'`) and resolves dependencies fresh, on purpose: that is what someone
running `pip install talkpipe-vault` gets, so the build breaks when *they*
would break. A dependency problem that only the lockfile hides is one we want
CI to see.

Two consequences worth remembering:

- `uv.lock` is a development convenience. It pins nothing for users and is not
  a security control — the version floors in `pyproject.toml` are what
  actually protect an install. Fix a vulnerable dependency by raising its
  floor, not by refreshing the lock.
- The lock must still stay honest. CI runs `uv lock --check`, which installs
  nothing and fails only when `uv.lock` and `pyproject.toml` have drifted
  apart. If you change dependencies, run `uv lock` and commit the result.

## Releasing

The release process — tag conventions, the manual application test that
must pass before tagging, and the publish steps — is in
[RELEASING.md](RELEASING.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Authors

- **Travis Bauer** — *Initial development* — [Sandia National Laboratories](https://www.sandia.gov/)

## Acknowledgments

Built with [TalkPipe](https://github.com/sandialabs/talkpipe); vector storage
by [LanceDB](https://lancedb.com/); file monitoring with
[Watchdog](https://github.com/gorakhargosh/watchdog).
