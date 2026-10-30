<p align="center">
  <img src="docs/talkpipe_vault.jpg" alt="TalkPipe Vault Logo" width="300">
</p>

<h1 align="center">TalkPipe Vault</h1>

<p align="center"><em>Point it at folders of documents and ask questions.<br>
Indexed on your machine, answered by the model you choose.</em></p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11.4+-blue.svg" alt="Python 3.11.4+"></a>
  <a href="https://opensource.org/licenses/Apache-2.0"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <a href="https://github.com/sandialabs/talkpipe-vault"><img src="https://img.shields.io/badge/status-alpha-yellow.svg" alt="Development Status"></a>
</p>

## In short

**The problem.** Documents pile up: notes, reports, papers, manuals, saved
web pages. Finding something in them means remembering which file it was in
or the exact words it used, and asking an AI chatbot about them usually
means uploading them to someone else's service.

**How TalkPipe Vault solves it.** Point it at a folder and it builds a
search index on your computer that captures what each passage means.
Search by meaning ("what should I do if I meet a bear?" finds the advice to
back away slowly), or ask a question and get an answer that cites its
sources so you can check them. An optional agent mode lets the AI search
several times before it answers.

**Why use it.**

- **Private:** your documents stay on your computer. Only your question and
  the passages needed to answer it go to the AI model you choose, and with
  a model on your own machine (Ollama), nothing leaves it at all.
- **Fast:** the default indexing uses a small built-in model that runs on an
  ordinary processor, with no graphics card or model server. It is far
  faster than the large AI models many similar tools index with, so a
  typical folder of notes is searchable in minutes.
- **Low upkeep:** it follows your files as they change, needs no account or
  cloud service to build its index, runs in a browser or a terminal, and
  installs with one command.
- **Built to be reused:** the vault is assembled from standalone
  components, its own indexing, search and agent retrieval pieces (see the
  [Developer Guide](docs/DEVELOPER_GUIDE.md)) as well as those of the
  [TalkPipe](https://github.com/sandialabs/talkpipe) and
  [talkpipe-agents](https://github.com/sandialabs/talkpipe-agents)
  libraries it builds on. Each is tested on its own, so you can test and
  reuse any of them in your own applications, and every application that
  uses them helps harden them over time.

<p align="center">
<img src="docs/vault-homepage.png" alt="Talkpipe Homepage" width="100%">
</p>
<p align="center">
<img src="docs/vault-search.png" alt="Semantic Search with 'Let an agent select the results' checked: the agent's note and the results it kept" width="100%">
</p>
<p align="center">
<img src="docs/vault-ask.png" alt="Ask with 'Use Agent' checked: a cited answer and the agent's outcome" width="100%">
</p>

## What is TalkPipe Vault?

TalkPipe Vault indexes folders of documents (notes, papers, reports, an
Obsidian vault, a project archive) into a local search index and lets you
explore them three ways, in a browser or in a terminal:

- **Semantic search**: find documents by meaning, not just words.
- **Keyword search**: precise full-text queries with boolean operators.
- **Ask**: single-turn Q&A with answers grounded in your documents.

Indexing happens on your machine: the built-in embedding model runs
in-process with no server or API key, and the indexes live next to your
documents. Answers come from the chat provider you choose: an
[Ollama](https://ollama.com/) server, OpenAI, or Anthropic (see
[LLM providers](#llm-providers)).

It is built on the [TalkPipe](https://github.com/sandialabs/talkpipe)
pipeline framework, and its indexing and search components are reusable in
your own pipelines; see the [Developer Guide](docs/DEVELOPER_GUIDE.md).

**Status:** alpha, under active development. This page describes the
`master` branch; if something here is missing from the latest release,
install the development branch with
`pip install "git+https://github.com/sandialabs/talkpipe-vault.git@master"`
(the `@master` matters: the default branch, `stable`, is the latest
release).

## Run the web app

### Option 1: Install the package

Install it either as a standalone application with uv or into a virtual
environment with pip; everything after the install is the same.

**With uv, as a standalone application.**
[uv](https://docs.astral.sh/uv/)'s `uv tool install` gives the vault an
environment of its own and puts its commands (`vault-server`, `vault-tui`
and `vault-index`) on your PATH, so there is no virtual environment to
create or activate:

```bash
uv tool install talkpipe-vault
vault-server ~/notes        # any folder of documents
```

- If uv warns that its tool directory is not on your PATH, run
  `uv tool update-shell` and open a new terminal.
- uv picks a supported Python and downloads one if none is installed; to
  choose one, add `--python 3.12`.
- `uv tool upgrade talkpipe-vault` upgrades it and
  `uv tool uninstall talkpipe-vault` removes it. Your settings and indexes
  are kept.
- To try it without installing anything:
  `uvx --from talkpipe-vault vault-server ~/notes`.

**With pip, into a virtual environment:**

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install talkpipe-vault
vault-server ~/notes        # any folder of documents
```

Your browser opens at http://127.0.0.1:8002 with `~/notes` being indexed in
the background (on a headless machine add `--no-browser` and open the address
it prints). Search and ask right away; the pages say how far indexing
has got. Started again later, `vault-server` reopens the folders you had
open. Without a path, add folders on the **Vaults & Documents** page. A
path that does not exist is created as a new, empty named vault and the
startup banner says so; if you meant a folder of documents, check the
spelling.

The first index downloads the default embedding model from Hugging Face
(about 130 MB, cached afterward). Search and indexing need nothing more.
**Ask** needs a chat provider: pick it on the **Settings** page and enter
its server URL or API key there. Until one is reachable, Ask shows a
connection error that says how to fix it.

### Option 2: Container (Podman or Docker)

```bash
podman run --rm -p 8002:8002 \
  -v vault_data:/app/data \
  -v ~/Documents:/documents:ro,Z \
  -e TALKPIPE_OLLAMA_SERVER_URL=http://host.containers.internal:11434 \
  ghcr.io/sandialabs/talkpipe-vault:latest
```

Then open http://127.0.0.1:8002 (`127.0.0.1`, not `localhost`: rootless
podman publishes ports IPv4-only). `docker run` takes the same arguments.
`latest` is the current release; when a pre-release is out it is published
under the `experimental` tag.

- `-v vault_data:/app/data` keeps settings, indexes kept in app data and the
  embedding-model cache across container recreation.
- `-v ~/Documents:/documents:ro,Z` is what the folder picker can see. Drop
  `:Z` on macOS and Windows; keep it on SELinux hosts such as Fedora.
- `-e TALKPIPE_OLLAMA_SERVER_URL=…` is optional and only for Ollama running on
  the host. For OpenAI or Anthropic, drop it and enter the key on the
  Settings page instead.

Compose, a customized image, and podman machine memory on macOS and Windows
are in the [Advanced Guide](docs/ADVANCED.md#containers).

## Folders

Choose the folders you want to ask about: on the **Vaults & Documents**
page, on the terminal interface's Vault tab, or on the command line.

```bash
vault-server ~/notes                      # or: vault-server --dir ~/notes --dir ~/papers
```

There is no vault to name or create. For each folder, and everything under
it:

- **Indexing runs in the background**, keywords first (searchable within
  minutes), then meaning. Every page says how far it has got, and keyword
  matches cover files not embedded yet.
- **Changes are picked up as they happen**: new, changed, renamed and
  deleted files.
- **Some files are skipped**: hidden files and folders (`.git`, `.obsidian`,
  …), editor temporary files, and formats no extractor reads.
- **Quitting loses nothing**: indexing resumes where it left off, and a
  second vault-server (`--port 8003`) or vault-tui on the same folder
  shares the index. Run `vault-server ~/papers` while one is up and the
  running instance adds the folder and opens in your browser.
- **Several folders** can be open together; every result names its folder.

The index lives in a hidden `.tpvault` folder inside each folder (git and
most backup tools skip it), or in the app's data directory when the folder
is read-only, on a network share or synced by a cloud client; see
[Folder mode](docs/ADVANCED.md#folder-mode). **Close** a folder to stop
searching and indexing it while it stays in the list, and **Open** it again
later; **Remove** drops it from the list and keeps its index unless you ask
for it to be deleted. Your documents are never touched.

To index a folder without starting an interface (a cron job, a build step),
run `vault-index ~/notes`; it processes what changed and exits
([Advanced Usage](docs/ADVANCED.md#vault-index)). To index, watch or search
folders from Python or ChatterLang, or to follow a folder's changes as a
stream of documents, see the [Developer Guide](docs/DEVELOPER_GUIDE.md).

### Named vaults

A named vault is an index kept in a separate folder of its own, for
documents you would rather not index in place. It lives under **Named
vaults (advanced)** on the Vaults & Documents page: pick the documents (a
folder or glob pattern), accept the suggested vault name, and one click
creates the vault and indexes into it. Opening a named vault closes the
folders you had open (the two do not mix); add a folder again to go back.
A named vault's keyword index is
built once with a button on the Keyword Search page and does not refresh
when you index more documents; rebuild it there when you do.

## The web interface

- **Vaults & Documents**: the folders being searched, with each one's
  progress, and the named-vault controls.
- **Semantic Search**: the ten closest chunks. **Let an agent select the
  results** hands the search to an agent that searches, reads, keeps what is
  relevant and stops when it judges it could answer your question; slower,
  and it needs a chat model that can call tools. Details and which models
  do well are in [Agent search](docs/ADVANCED.md#agent-search).
- **Keyword Search**: boolean and phrase queries. Matching is on exact word
  tokens (`apple` does not match `apples`); use semantic search for meaning.
  A file's full path is searched along with its text, so a word in the path
  to your folder (your user name, `Documents`, a year) matches every file;
  write `content:word` to search the text only.
- **Ask**: a single question, answered with citations you can open and copy.
  **Boost retrieval with keyword search** merges keyword matches into the
  context; **Use Agent** lets the agent gather the context instead, and
  **Extra Effort** gives it twice the iterations for questions that need
  one fact to find the next. While the agent works, the agent's latest step
  (a search, what it kept) shows under the progress indicator. The "Answered
  by" line says what ran.
- **Settings**: embedding and chat providers and models, the agent's
  limits, a **Configuration status** panel that tests the selection, and
  **Connections & credentials** for API keys and server URLs.

Every result and citation has an **Open** link that streams the original
document from the server, so it works the same from a container.

## Agent search is built on talkpipe-agents

The agent options (**Let an agent select the results** on Semantic Search
and **Use Agent** on Ask, each with **Extra Effort**, in the browser and the
terminal) use [talkpipe-agents](https://github.com/sandialabs/talkpipe-agents).
talkpipe-agents is a TalkPipe plugin that puts a tool-using agent at one
bounded step of an otherwise deterministic pipeline. In the vault, that
step is retrieval. The agent searches, each search's hits are judged
before it sees them, it keeps what is relevant, and it stops when it judges
it has enough to answer. The answer is then written and cited by the same
pipeline stages a plain Ask uses.

The agent machinery is independent of the vault. talkpipe-agents provides
the `llmAgent` segment, the
[findings loop](https://github.com/sandialabs/talkpipe-agents/blob/stable/docs/findings.md)
that judges, keeps and ranks what tools return (`FindingsSession`,
`FindingsJudge`, `finding_tool`), the tool registry and the session tools
such as `enough_information`, and none of it knows about vaults, documents
or this application. The vault adds only what is its own: search tools over
its indexes, a retrieval prompt, and the pages that show the outcome. You
can use the same mechanisms in other applications, with a database, an API,
a code base or anything else you can wrap as a tool, by installing
talkpipe-agents (`pip install talkpipe-agents`) without the vault. The vault's
search tools also work behind an agent of your own; see the
[Agent retrieval developer's guide](docs/AGENT_RETRIEVAL.md).

## The terminal interface (`vault-tui`)

The same features in a terminal (SSH, tmux, a headless machine), installed
with the package. It runs in-process and shares the web interface's
folders, settings and credentials.

```bash
vault-tui ~/notes         # search a folder of documents (add more with --dir)
vault-tui ~/my-vault      # open (or create) a named vault
vault-tui                 # reopen the last session's folders, else choose on the Vault tab
```

| Key | Tab | What you can do |
|-----|-----|-----------------|
| `F2` | Vault | Add and remove folders, open or create a named vault, index documents, edit the retrieval filter |
| `F3` | Search | Semantic search; `Enter` shows the chunk, `o` opens the source, `c` copies; **Use Agent** as in the browser |
| `F4` | Keywords | Full-text search |
| `F5` | Ask | Question answering with citations; keyword boost or **Use Agent** |
| `F6` | Settings | Configuration status, models, connections and credentials |
| `F1` / `Ctrl+R` / `Ctrl+Q` | | Help / reload / quit |

Ask needs a chat provider here too: choose it on the Settings tab and enter
its key or URL under **Connections & credentials**. More keys and behaviour
are in the [Advanced Guide](docs/ADVANCED.md#terminal-interface-notes).

## LLM providers

Two models are used: one for **embeddings** (indexing and semantic search)
and one for **chat** (Ask). Each can come from any provider TalkPipe
registers for that role:

| Provider | Embeddings | Chat | What it needs |
|----------|------------|------|---------------|
| `model2vec` | **default** (`minishlab/potion-retrieval-32M`) | no | Nothing: runs in-process; the model downloads once |
| `ollama` | yes | **default** (`mistral-small`) | A reachable Ollama server with the model pulled |
| `openai` | yes | yes | An API key; optionally a base URL for an OpenAI-compatible endpoint |
| `anthropic` | no | yes | An API key |
| `eliza` | no | plumbing check only | Nothing; its replies ignore your documents |

Your documents and indexes stay on your disk. Each question, together with
the snippets retrieved to answer it, goes to the chat provider you
configure; nothing else is sent anywhere, and with `model2vec` embeddings
and a local Ollama server nothing leaves your machine at all.

Pick the source and model for each role on the **Settings** page; the
choice persists and the **Configuration status** panel tests it. Enter keys
and URLs under **Connections & credentials** there, or set
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY` or
`TALKPIPE_OLLAMA_SERVER_URL` in the environment (a value saved in Settings
wins). Chat models can change at any time. Embeddings cannot: a folder
records the embedder it was indexed with and keeps using it for new files
and for searches, even after you pick a different one in Settings. Folders
indexed later get the new embedder, and a search across folders with
different embedders still works (each is queried with its own model and the
hits are fused). To move a folder to the new embedder, use its **Re-embed**
action. An index built before the embedder was recorded falls back to the
Settings embedder, so re-embed it if its results look wrong. The full
reference is in the [Advanced Guide](docs/ADVANCED.md#model-configuration).

## More documentation

- [Advanced Usage](docs/ADVANCED.md): the `vault-server` and `vault-tui`
  flag reference, confining paths on a shared machine, terminal interface
  notes, containers (compose, a customized image, macOS and Windows), model
  configuration and provider notes, agent search, retrieval filters, and
  where indexes live on disk.
- [Developer Guide](docs/DEVELOPER_GUIDE.md): every source and segment the
  vault registers (`indexFolder`, `watchFolders`, `folderChanges`,
  `vaultSearch`, `vaultChat`, …) with Python and ChatterLang examples, recipes
  for pipelines of your own, the Python helpers, and contributing.
- [Agent retrieval developer's guide](docs/AGENT_RETRIEVAL.md): the agent's
  tools, and using them in your own application.

## Requirements

- **Python** 3.11.4+ (pip install path)
- **A chat provider** for Ask: a local Ollama server, an OpenAI API key (or
  OpenAI-compatible endpoint), or an Anthropic API key. Indexing and search
  need no chat provider.

## Contributing

Contributions are welcome. Before submitting: `pytest` passes, and
`ruff check .`, `ruff format --check .`, and `mypy` are clean -- CI fails on
any finding from those three (`ruff check --fix . && ruff format .` fixes
most; `pre-commit install` runs them on every commit). See
[Contributing to the vault](docs/DEVELOPER_GUIDE.md#contributing-to-the-vault).

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
