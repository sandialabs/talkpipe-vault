# TalkPipe Vault — Advanced Guide

Reference material beyond [getting the web app running](../README.md):
command-line tools, container customization, the full model-configuration
reference, architecture, reusable pipeline components, and development setup.

## Command-line tools

The package installs one console script, `vault-server`. Indexing from the
command line uses TalkPipe's `makevectordatabase`, installed with the
TalkPipe dependency.

### `makevectordatabase`

Builds the LanceDB `docs` table that `vault-server` reads — the same layout
the **Add Documents** page produces. Embedding configuration is required:
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
vault-server [~/my-vault] [--resume] [--host 0.0.0.0] [--port 8002] [--show-source-paths] [--no-browser]
```

- The vault path is optional; without it the app starts on the Vaults page.
- `--resume` opens the vault most recently used in the web interface,
  falling back to the given path (or the Vaults page) when no recent vault
  is usable. This is how the container image starts.
- `--show-source-paths` shows source file paths in search results and serves
  those files over HTTP; hidden by default.
- `--no-browser` skips opening a browser on startup (headless servers,
  services, containers).

The app module can also be run directly with the same options:

```bash
python -m talkpipe_vault.apps.query ~/my-vault --host 0.0.0.0 --port 8002
```

## Containers

The [README](../README.md#option-2-container-podman-or-docker) covers the
basic `podman run` invocation. Additional options:

### Compose

`docker-compose.yml` provides the same setup as a compose service,
configured through `.env` (see `.env.example`):

```bash
pip install podman-compose   # podman needs a compose provider
podman compose up -d
```

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

# Different default sources/models for embeddings and chat. TalkPipe reads
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

- Ollama-backed sources still need `TALKPIPE_OLLAMA_SERVER_URL` at run time.
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
chat_model = "gpt-4"
chat_source = "openai"
```

Or via environment variables:

```bash
export TALKPIPE_EMBEDDING_MODEL="text-embedding-3-large"
export TALKPIPE_EMBEDDING_SOURCE="openai"
export TALKPIPE_CHAT_MODEL="gpt-4"
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
| `retrieval_template` | `RETRIEVAL_TEMPLATE` | Formats search queries before embedding. Placeholder: `{query}` | `"task: search result \| query: {query}"` |

### Provider notes

- **model2vec** (default, embeddings only): runs fully in-process — no
  server, no API key. The model downloads from Hugging Face on first use and
  is cached; use TalkPipe's `talkpipe_precache_model2vec` to prefetch for
  offline machines.
- **Ollama**: set the server URL in **Settings → Connections & credentials**,
  via `TALKPIPE_OLLAMA_SERVER_URL`, or with `OLLAMA_SERVER_URL` in
  `~/.talkpipe.toml` (default `http://localhost:11434`). The model must
  already be pulled on that server.
- **OpenAI**: set `OPENAI_API_KEY` in the environment or enter it in the
  Settings page.
- **Anthropic** (chat only): model name such as `claude-sonnet-4-5`; key via
  Settings page or `ANTHROPIC_API_KEY`.
- **eliza** (chat only, built-in): a rule-based responder needing no server
  or key — useful for smoke-testing the Ask page before a real chat provider
  is configured. It does not use the retrieved context.
- **Plugins**: the Settings dropdowns are populated from TalkPipe's provider
  registry, so a TalkPipe plugin that registers an additional embedding or
  chat provider appears there automatically once installed. See
  [Deriving a customized image](#deriving-a-customized-image) for installing
  plugin packages in the container.

### Overriding in code

Explicit parameters take precedence over all configuration. Note that
calling a segment factory only *constructs* a pipeline segment — nothing is
indexed until items are fed through it and consumed:

```python
from talkpipe_vault.pipelines.building_and_watching import build_vector_db_from_paths

indexer = build_vector_db_from_paths(
    vault_path="/path/to/vault",
    embedding_model="minishlab/potion-retrieval-32M",
    embedding_source="model2vec",
)

items = [{"path": "/path/to/documents/notes.txt"}]
for chunk in indexer(items):
    print(f"Indexed chunk: {chunk['shingle_id']}")
```

## Architecture

TalkPipe Vault is built on [TalkPipe](https://github.com/sandialabs/talkpipe),
a Python framework for composable data pipelines, and provides custom
TalkPipe sources and segments for document processing and vector database
creation.

**Technology stack:** TalkPipe (pipelines), LanceDB (vector search), Whoosh
(full-text search), Watchdog (filesystem events), FastAPI + Jinja2 (web),
model2vec / Ollama / OpenAI / Anthropic (models).

The stable path: the **Add Documents** page (or `makevectordatabase`) builds
a LanceDB `docs` table via TalkPipe's document pipeline — read, chunk,
shingle, embed, store — and `vault-server` searches it.

### Registered TalkPipe components

**Sources:**

- `fileWatcher` — file system event monitoring; experimental
- `watchIntoVectorDB` — combined watching and vector database creation;
  experimental
- `listIntoVectorDB` — batch processing from glob patterns for the
  experimental vault layout

**Segments:**

- `buildVectorDBFromPaths` — complete document processing pipeline
- `vaultSearch` — semantic search on a vault's vector database
- `vaultTextSearch` — full-text keyword search using the Whoosh index
- `vaultChat` — RAG-based Q&A using vault contents

### Building your own pipelines

TalkPipe's strength is composability — complex functionality built from
simple pipeline operators.

> **Note:** Examples 1–3 use the **experimental** watcher/list components.
> They are useful for understanding composition, but the watcher pipelines
> write the experimental `full_documents`/`shingled_chunks` layout, **not**
> the `docs` table that `vault-server` reads. For the stable path, index
> with `makevectordatabase` or the **Add Documents** page.

**Example 1: Simple file watching pipeline**

```python
from talkpipe_vault.watchdog import file_watcher
from talkpipe.pipe.io import Print

# Watch a directory and print events
pipeline = file_watcher(path="/path/to/watch") | Print()

# Run the pipeline. file_watcher runs until interrupted (Ctrl+C), so this
# loop does not return on its own — each iteration yields one event.
for event in pipeline():
    pass
```

**Example 2: Complete document processing**

`ReadFile` stores an `ExtractionResult` object (with `.content`, `.source`,
`.title` fields) in the target field, not a plain string — filter
expressions must go through `.content` to reach the text:

```python
from talkpipe_vault.watchdog import file_watcher
from talkpipe.data.extraction import ReadFile
from talkpipe.pipe.basic import FilterExpression
from talkpipe.pipelines.vector_databases import MakeVectorDatabaseSegment

pipeline = \
    file_watcher(path="/path/to/watch") | \
    FilterExpression(expression="item['event'] != 'deleted'") | \
    ReadFile(field="path", set_as="full_content") | \
    FilterExpression(expression="len(item['full_content'].content.strip()) > 0") | \
    MakeVectorDatabaseSegment(
        path="~/my-vault",
        embedding_model="mxbai-embed-large:latest",
        embedding_source="ollama",
        embedding_field="full_content",
        table_name="documents",
        doc_id_field="path"
    )

for result in pipeline():
    print(f"Processed: {result['path']}")
```

Two practical notes: Ollama components read the server URL from
`TALKPIPE_OLLAMA_SERVER_URL` (or `OLLAMA_SERVER_URL` in `~/.talkpipe.toml`,
default `http://localhost:11434`). Also expect a single file save to produce
more than one event (typically `created` then `modified`), so a file can be
processed twice in a row; the vector database deduplicates by
`doc_id_field`, so the end state is still one record per file.

**Example 3: Using registered components via configuration**

Registered sources and segments can be referenced by name from a TalkPipe
**chatterlang** script, compiled with `talkpipe.compile`. This batch example
indexes a glob of files with the experimental `listIntoVectorDB` source
(note the parameter is `source_pattern`, not a directory path):

```python
import talkpipe

# Registered components are discovered from installed plugins.
talkpipe.load_plugins()

pipeline = talkpipe.compile(
    'INPUT FROM listIntoVectorDB['
    '  source_pattern="/path/to/documents/**/*.txt",'
    '  vault_path="~/watched-vault",'
    '  embedding_source="model2vec",'
    '  embedding_model="minishlab/potion-retrieval-32M",'
    '  overwrite=True'
    ']'
)

# Run it. Each yielded item is an indexed chunk
# (keys: shingle_id, shingle, source, title).
chunks = list(pipeline())
print(f"Indexed {len(chunks)} chunk(s) into the vault.")
```

To watch a directory instead of a fixed glob, use the `watchIntoVectorDB`
source (parameters `source_path` and `polling`) in place of
`listIntoVectorDB` — it runs until interrupted. Both write the experimental
layout rather than the `docs` table used by `vault-server`.

## Vault storage structure

**Stable layout** (what `vault-server` reads) — produced by the **Add
Documents** page and by `makevectordatabase`:

- `docs` — LanceDB table of chunk embeddings
- `fulltext_vault/` — Whoosh full-text index, created on demand from the
  **Keyword Search** page
- `vault_metadata.json` — records the embedding source/model (and vector
  dimension) the vault was indexed with, so reopening the vault
  automatically uses a matching embedder; it travels with the vault if the
  folder is copied or moved. Written by the **Add Documents** page; vaults
  built with `makevectordatabase` alone don't have it and are treated as
  legacy vaults.

**Experimental watcher layout** — produced by the in-development pipelines
in `pipelines/building_and_watching.py` (`listIntoVectorDB` /
`watchIntoVectorDB`). These tables are **not** read by `vault-server`:

- `full_documents` — embeddings for templated full-document content
- `shingled_chunks` — embeddings for overlapping chunk windows with
  composite ids like `first-last-source`
- `fulltext_vault/` — Whoosh index over full document content

## Experimental directory monitoring

Directory monitoring is not the recommended production path yet; the watcher
components should be treated as experimental until the indexing and serving
paths are unified. The old `vault-watch-into-vectordb` and
`vault-list-into-vectordb` console scripts are not installed; their helper
functions remain for development testing:

```bash
python -c "from talkpipe_vault.pipelines.cli import watch_vectordb_main; watch_vectordb_main()" \
    /path/to/documents \
    --vault-path ~/watched-vault \
    --patterns "*.txt" "*.md" \
    --polling \
    --overwrite

python -c "from talkpipe_vault.pipelines.cli import list_vectordb_main; list_vectordb_main()" \
    "/path/to/documents/**/*.txt" \
    --vault-path ~/watched-vault \
    --overwrite
```

Expectations: the watcher reacts only to filesystem events that happen
**after** it starts — pre-existing files are not indexed (use
`list_vectordb_main` for those). Each indexed chunk is printed as a
`{'shingle_id': ...}` line; results land in the experimental vault layout
described above.

## Development setup

```bash
git clone https://github.com/sandialabs/talkpipe-vault.git
cd talkpipe-vault

# Virtual environment (see the README note about PEP 668)
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -e ".[dev]"

# Tests
pytest
pytest --cov=src --cov-report=term-missing --cov-report=html

# Quality (CI runs these)
black src/ tests/
isort src/ tests/
flake8 src/ tests/
mypy src/          # advisory: CI allows failures; avoid new errors

# Security scanning
bandit -r src/
safety check
```

### Project structure

```
talkpipe-vault/
├── src/
│   └── talkpipe_vault/
│       ├── pipelines/
│       │   ├── building_and_watching.py    # Experimental watcher pipelines
│       │   ├── searching_and_prompting.py  # Search and RAG segments
│       │   ├── config.py                   # Config resolution + vault layout
│       │   └── cli.py                      # Experimental watcher CLI helpers
│       ├── apps/
│       │   ├── query.py                    # Web application
│       │   ├── vault_server.py             # vault-server CLI entry point
│       │   ├── user_settings.py            # Persisted UI settings
│       │   ├── templates/                  # HTML templates
│       │   └── static/                     # Static assets
│       └── watchdog.py                     # File system monitoring
├── docs/                                   # Images and this guide
├── tests/                                  # Test suite
└── pyproject.toml                          # Package configuration
```
