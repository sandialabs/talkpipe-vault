<p align="center">
  <img src="docs/talkpipe_vault.jpg" alt="TalkPipe Vault Logo" width="300">
</p>

# TalkPipe Vault

> Turn folders of documents into a searchable, question-answerable vault — on your own machine.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
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
runs in-process with no server or API key; for generated answers you can point
it at a local [Ollama](https://ollama.com/) server or a cloud provider
(OpenAI, Anthropic) — your documents are only ever sent to the provider you
choose.

It is built on the [TalkPipe](https://github.com/sandialabs/talkpipe)
pipeline framework and doubles as a real-world example of composing document
processing, vector search, and RAG from reusable components — see the
[Advanced Guide](docs/ADVANCED.md) if that side interests you.

**Status:** alpha, under active development.

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

Open http://127.0.0.1:8002, then:

1. **Vaults** — create a vault (any new or empty folder; it holds the index,
   not your documents).
2. **Add Documents** — pick a folder or glob pattern to index. The first
   index downloads the default embedding model from Hugging Face (~30 MB,
   cached afterward).
3. **Search** and **Ask** away.

Answers on the Ask page need a chat model: point the app at an Ollama server
under **Settings → Connections & credentials** (or set
`TALKPIPE_OLLAMA_SERVER_URL`), or configure OpenAI/Anthropic there. With no
chat provider at all, Ask falls back to a built-in scripted responder
(eliza) that is only useful for checking that the plumbing works.

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
  you last used; the first run starts on the Vaults page.
- `-v ~/Documents:/documents:ro,Z` — host documents to index; the folder
  picker only sees what you mount. Mount `~` instead to browse your whole
  home directory. Keep `:Z` on SELinux Linux hosts (e.g. Fedora); **drop it
  on macOS and Windows**, where it makes podman try to relabel every mounted
  file.
- The `TALKPIPE_OLLAMA_SERVER_URL` line is **optional** — without it, search
  and indexing still work and Ask falls back to the scripted responder.

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

## The web interface

- **Vaults** — create or choose a vault with a built-in folder browser;
  recent vaults are remembered for one-click reopening.
- **Add Documents** — index a folder or glob into the current vault, with
  live progress.
- **Settings** — choose embedding and chat providers/models, with a live
  **Configuration status** panel that tests your selection (and can download
  an uncached embedding model via Re-test), plus **Connections &
  credentials** for API keys and the Ollama URL — no environment variables
  required.
- **Semantic Search** — vector similarity search over your documents.
- **Keyword Search** — boolean and phrase queries. Matching is
  case-insensitive but on exact word tokens (`apple` won't match `apples`);
  use semantic search for meaning-based lookups.
- **Ask** — single-turn Q&A with source citations you can open and copy.

## Configuring models

The Settings page is the primary way to configure models; choices persist
and apply immediately. Defaults: embeddings run in-process via
model2vec (`minishlab/potion-retrieval-32M`), chat uses `ollama` /
`mistral-small`. OpenAI and Anthropic are supported via API keys entered in
the browser.

One behavior worth knowing: the embedding model is a property of the indexed
data — embeddings are only comparable to queries embedded by the same model —
so each vault records the embedder it was built with and reopens with it,
regardless of the current default. Chat models can be switched freely at any
time.

Configuration is also possible via `~/.talkpipe.toml` or `TALKPIPE_*`
environment variables; the full reference (precedence, all keys, templates,
provider notes) is in the [Advanced Guide](docs/ADVANCED.md#model-configuration).

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
- **Ollama** (optional) for local chat answers; **OpenAI/Anthropic API key**
  (optional) for cloud models. Embeddings work out of the box with neither.

## Contributing

Contributions are welcome. Before submitting: `pytest` passes,
`black src/ tests/ && isort src/ tests/` applied, `flake8 src/ tests/` clean,
and no new `mypy src/` errors (CI runs it but allows failures). See
[Development setup](docs/ADVANCED.md#development-setup).

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Authors

- **Travis Bauer** — *Initial development* — [Sandia National Laboratories](https://www.sandia.gov/)

## Acknowledgments

Built with [TalkPipe](https://github.com/sandialabs/talkpipe); vector storage
by [LanceDB](https://lancedb.com/); file monitoring with
[Watchdog](https://github.com/gorakhargosh/watchdog).
