# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding conventions
- When commenting segments or sources, do not include the parameters in the code comment.
  Include a description of the data structure that it expects.
- For sources and segments, parameters should be defined using the Annotated typing convention.

## Project Overview

TalkPipe Vault is an AI-powered personal information assistant: a web application for turning
folders of documents into a searchable vault (semantic search, keyword search, RAG Q&A). It is
built on the TalkPipe framework and demonstrates real-world usage of its composable pipelines.

- **Package**: `talkpipe-vault`
- **Module**: `talkpipe_vault`
- **Python**: 3.11.4+ required
- **TalkPipe**: >=0.12.4 (`talkpipe[all]`)
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
│   ├── vault_server.py         # vault-server CLI entry point
│   ├── user_settings.py        # Persisted UI settings (recent vaults, model overrides)
│   ├── templates/              # Jinja2 templates (base, home, vaults, documents,
│   │                           #   settings, search, keyword_search, chat, partials)
│   └── static/                 # favicon.svg, logo.jpg
└── pipelines/
    ├── config.py               # Model/template config resolution + vault layout helpers
    ├── cli.py                  # Experimental watcher CLI helpers (not installed as scripts)
    ├── building_and_watching.py # Experimental watch/batch vector DB pipelines
    └── searching_and_prompting.py # VaultSearch / VaultChat / VaultTextSearch segments
```

## The Web Application (primary user path)

`vault-server [vault_path] [--host] [--port] [--show-source-paths]` — the vault path is
optional; without it the UI starts on the Vaults page.

Routes in `apps/query.py`:
- `/vaults` (+ `/vaults/open`): create or choose a vault; recents persisted via
  `user_settings.py` in `$TALKPIPE_VAULT_HOME` (default `~/.talkpipe-vault`).
- `/documents` (+ `/documents/index`): index a folder/glob into the current vault using
  TalkPipe's `build_rag_database()` driver (requires talkpipe >= 0.13.0b2; writes the
  `docs` table — the same driver behind TalkPipe's `makevectordatabase` CLI, with
  embedder preflight, dimension checking, overflow truncation, and skip counting).
- `/settings`: embedding + chat source/model overrides, persisted and applied immediately.
- `/`, `/search`, `/keyword-search` (+ index creation), `/chat`, `/chunk-content`,
  `/source-file`, `/refresh`.
- Pages that need a vault redirect to `/vaults` when none is selected.

State lives in the module-level `_state: AppState` singleton; pipelines are rebuilt by
`_refresh_pipelines()` (throttled to every 5s unless forced).

## Model Configuration Precedence (highest to lowest)

1. Explicit parameters passed to segments/sources
2. Web-interface Settings page (`$TALKPIPE_VAULT_HOME/settings.json`)
3. TalkPipe configuration (`~/.talkpipe.toml` or `TALKPIPE_*` env vars; also accepts
   `default_embedding_model_name`/`default_model_name` style keys)
4. Defaults in `pipelines/config.py`: model2vec/minishlab/potion-retrieval-32M (embeddings, in-process),
   ollama/mistral-small (chat)

Ollama server URL comes from `TALKPIPE_OLLAMA_SERVER_URL` (or `OLLAMA_SERVER_URL` in
`~/.talkpipe.toml`) — not `OLLAMA_BASE_URL`, which is meaningless to TalkPipe.

**Path fences** (`apps/access_control.py`): `TALKPIPE_VAULT_ROOT` confines vault
create/open/delete to one directory; `TALKPIPE_DOCUMENT_ROOTS` (os.pathsep-separated)
confines the folder picker and indexing. Unset/empty = unrestricted (the desktop
default); the container image sets `/app/data` and `/documents`. Enforced server-side
in `/api/directories`, `/vaults/open`, `/vaults/delete`, and `/documents/index`
(resolve-then-`is_relative_to`, so symlink escapes are caught); a configured root that
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
- The experimental watcher pipelines in `building_and_watching.py` write different tables
  (`full_documents`, `shingled_chunks`) that the web app does NOT read; they are not part
  of the stable path and are not installed as console scripts.

## TalkPipe Sources/Segments (registered entry points)

Sources: `fileWatcher`, `watchIntoVectorDB`, `listIntoVectorDB` (watcher ones experimental).
Segments: `buildVectorDBFromPaths`, `vaultSearch`, `vaultChat`, `vaultTextSearch`,
`searchLance`.

## Development Commands

```bash
# Setup
pip install -e .[dev]

# Tests (Ollama-dependent tests skip unless a server is reachable; point at a
# remote server with TALKPIPE_OLLAMA_SERVER_URL — never commit a real URL/IP)
pytest
TALKPIPE_OLLAMA_SERVER_URL=http://<ollama-host>:11434 pytest

# Quality (CI runs these)
black --check src/ tests/
isort --check-only src/ tests/
flake8 src/ tests/ --count --select=E9,F63,F7,F82 --show-source --statistics
mypy src/          # allowed to fail in CI

# Security
bandit -r src/
safety check
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
podman build -t talkpipe-vault -f Containerfile .
podman run --rm -p 8002:8002 --userns=keep-id \
    -v <host-data-dir>:/app/data:Z -v <host-docs-dir>:/documents:ro,Z \
    -e TALKPIPE_OLLAMA_SERVER_URL=http://host.containers.internal:11434 \
    talkpipe-vault
```

Default CMD serves `/app/data/vault` on port 8002. Mount documents somewhere readable
(e.g. `/documents`) and index them from the Add Documents page. Compose services (`vault`,
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
1. Test (Python 3.11/3.12/3.13): flake8, black, isort, mypy (allowed to fail), pytest+coverage, Codecov
2. Security scan: Bandit + Safety
3. Container build: build/push to GHCR + Trivy scan
4. CodeQL analysis
5. Publish to PyPI on release

## Key Configuration Files

- `pyproject.toml`: package config, entry points, tool settings
- `Containerfile` / `docker-compose.yml`: container image and services
- `.env.example`: example container environment (uses `TALKPIPE_OLLAMA_SERVER_URL`)
- `.github/workflows/ci-cd.yml`: CI/CD pipeline
