# Changelog

## In Development

### Features

#### Per-Vault Retrieval Filter
- Added an advanced, per-vault **ChatterLang retrieval filter**: a script that filters or transforms retrieved results before they reach the Ask answer. Each result reaches the script as `{"doc_id", "score", "document"}` — emit it to keep it, drop it to filter it, modify it to transform it — so any registered segment (including LLM transforms) can prune noisy documents or rewrite them in flight. In `lambda`/`lambdaFilter` expressions the result is `item` (`item['document']`), as everywhere in TalkPipe; its top-level keys are also exposed as bare names.
- Edit it under **Vaults & Documents → Retrieval filter for this vault (advanced)**, a folded section inside the open vault's panel with Save/Validate/Remove, worked starter recipes you can insert with one click, and help describing the result structure. Scripts must be a single segment-only pipeline (no `INPUT FROM` source, loops, or forks); Validate reports compile errors without saving.
- The script lives in the vault folder (`retrieval_filter.tps`) so it travels with the vault, but **whether it runs is a per-machine choice made for that one vault**: the section names the vault it applies to, the flags are stored per vault path, and a filter is inert until enabled, so a vault received from someone else never executes its bundled script on open.
- A **Strict** option makes a filter fail closed — if the script errors while answering, the question fails instead of being answered from unfiltered results. Off by default (a broken relevance filter shouldn't take down Ask); turn it on when the filter removes sensitive content, where failing open would leak exactly what it exists to remove.
- With keyword search on, each retrieval stream is filtered **independently**: both streams over-fetch 3×, each is filtered and truncated back to its own limit, and only then are they merged and deduplicated — so surviving keyword hits can't be squeezed out by vector hits.
- The Semantic Search and Keyword Search pages show an **Apply custom transform** checkbox (unchecked by default) when the vault has an active filter, and report how many results survived.
- Ask's meta line always states the filter outcome — applied, failed and skipped (with the reason), or present but not compiling — so filtered retrieval is never silently different from what you asked for. A filter that stops compiling is reported on the Configuration status panel and retrieval keeps working unfiltered.
- Added the `filterSearchResults` segment, which runs a ChatterLang script over a list of search results held on a field.

#### One Page for Vaults and Documents
- Merged the Vaults and Add Documents pages into a single **Vaults & Documents** page: choose the documents to index and the vault to index them into, then submit once. `/vaults` redirects there, and pages that need a vault now send you there too.
- With no vault open, choosing a documents folder suggests a vault name derived from it (`~/notes` → `~/notes-vault`, under `TALKPIPE_VAULT_ROOT` when set). The suggestion skips names whose folder already holds unrelated files and reuses an existing vault of that name, so a single submit creates the vault and indexes into it.
- With a vault open, the same form adds documents to it; "Use a different vault…" reveals the vault field to create or switch to another one for that run.
- Indexing refuses to use the folder being indexed as the vault, and validates the documents path before creating anything, so a typo no longer leaves an empty vault behind. Creating a vault in a folder that already holds other files still requires confirmation, and confirming resumes the indexing run.
- Laid the page out top to bottom: the documents step and the vault step are stacked, labelled cards instead of columns squeezed side by side, with the overwrite option and the submit button on their own row (and everything stacking on narrow windows). Delete/confirm buttons now render in the warning style they were meant to have.

#### Vault Management from the Web Interface
- Added a Vaults page to create a new vault or choose an existing one from the browser; recently used vaults are remembered (persisted under `~/.talkpipe-vault`, overridable with `TALKPIPE_VAULT_HOME`).
- Made `vault-server`'s vault path argument optional: starting with no arguments opens the vault manager so everything can be done from the browser.
- Added `--show-source-paths` to `vault-server`, matching the app module's flag.
- Pages that need a vault now redirect to the vault manager when none is selected, instead of showing empty results.

#### Document Indexing from the Web Interface
- Added an Add Documents page that indexes a folder (recursively) or glob pattern into the current vault using TalkPipe's document pipeline — the same pipeline behind `makevectordatabase`, so the resulting `docs` table matches what search and chat expect.
- Indexing reports the number of chunks indexed and the embedding model used, and fails with a clear message when the pattern matches no files or the files contain no readable content.

#### Model Configuration from the Web Interface
- Added a Settings page to configure the provider (source) and model for both embeddings and chat. Choices persist across restarts, take precedence over TalkPipe configuration, and apply to the search/chat pipelines immediately.
- Changing the embedding model warns that existing vaults must be re-indexed so stored and query vectors match.
- Added settings for chunk size, shingle size, shingle overlap, and Ask RAG result count; these persist across restarts and feed both document indexing and Ask retrieval.

### TalkPipe 0.14.1 Requirement

- Require `talkpipe[all]>=0.14.1`. The previous floor, `>=0.13.0b3`, named a
  beta; because the specifier itself referenced a prerelease, pip was free to
  resolve TalkPipe to future prereleases as well. The floor is now a stable
  release, so a plain `pip install talkpipe-vault` stays on stable TalkPipe.

### TalkPipe 0.12.4 Compatibility

- Require `talkpipe[all]>=0.12.4`.
- Updated all `makevectordatabase` examples to pass `--embedding_source`/`--embedding_model`, which TalkPipe now requires unless embedding defaults are set in its configuration.
- Test fixtures that build vaults via `makevectordatabase` now pass the configured embedding model/source explicitly and invoke the CLI via `python -m`, and the test suite's Ollama availability probe honors `TALKPIPE_OLLAMA_SERVER_URL` so the Ollama-dependent tests can run against a remote server.

### Web Interface Refresh

- Indexing now runs in the background with live progress in the browser: the Add Documents page shows a progress bar with the file count, the file currently being embedded, and the running chunk total (polling a new `/api/index-status` endpoint), then reports the final outcome. Only one indexing run can be active at a time; starting another while one is in progress shows a clear message, and returning to the page while a run is active resumes the progress display.
- Embeddings now default to model2vec (`minishlab/potion-retrieval-32M`), which runs fully in-process — no Ollama server or API key needed to index and search. The model is downloaded from Hugging Face on first use and cached. Chat answers still default to `ollama`/`mistral-small`, and all of it remains configurable on the Settings page. Vaults indexed with the previous `ollama`/`embeddinggemma` default must be re-indexed (Add Documents with Overwrite) or the embedding settings switched back before searching them.
- Choosing a vault or a documents folder no longer requires typing a path: a Browse button opens a folder-picker dialog that navigates the server's directories (backed by a new `/api/directories` endpoint), with a jump-to-home shortcut and, on the Vaults page, an optional new-folder name for creating a vault in the chosen location. Paths can still be typed or pasted directly.
- Submit buttons (Open or Create, Index Documents, Search, Keyword Search, Ask) stay disabled until their input has a value, so an accidental empty submission is no longer possible.

- Redesigned the web interface with a coherent design system: unified indigo/violet palette and typography, refreshed header with vault/chunk pills and navigation, consistent panels, buttons, form fields, flash banners, result cards, and empty states, and improved responsive behavior on small screens. Page templates now share the design system from the base template instead of carrying duplicate inline styles.
- Success and error notices now appear consistently across all pages, including the home page confirmation after opening a vault (previously dropped silently) and the keyword-search index messages.

### Bug Fixes

#### Watcher Shingle Indexing
- The experimental watcher pipelines (`watchIntoVectorDB`, `watch_vectordb_main`) now write each document's shingles to the `shingled_chunks` table as soon as the document is processed. Previously shingling was scoped to the whole stream, so a short document's chunks waited for the next file event or for the stream to end — which never happens for a watcher — and `shingled_chunks` was never populated. The watch helper also prints a `{'shingle_id': ...}` line per indexed chunk now, so processing is visible.

#### Ask Page Source Paths
- The Ask answer's "Sources:" list now shows only file names unless the server was started with `--show-source-paths`, matching the search pages, which already hid absolute filesystem paths by default.

#### Ask Answer Attribution
- Ask answers now say which provider and model generated them. When the built-in `eliza` smoke-test responder is selected, the attribution says so explicitly (including that it does not use your documents) — previously eliza's greeting quoted the configured model name (e.g. "I am mistral-small"), which made a scripted canned reply easy to mistake for a real model's answer.

#### Copy Buttons over Plain HTTP and in Firefox
- Every copy button (Copy All Results, Copy Chunk on search results and Ask sources, and the Ask answer's Copy) now works when the app is reached over plain http from another machine (e.g. `http://server:8002` on a LAN). The buttons called the asynchronous Clipboard API, which browsers only expose on secure origins (https or localhost), so outside those contexts every copy failed silently. A shared helper now falls back to the classic hidden-textarea `execCommand('copy')` path, and the buttons flash "Copy failed" instead of doing nothing when even the fallback is refused.
- Copy Chunk buttons fetch the full chunk from the server before copying, and the old handlers only wrote to the clipboard after that fetch resolved. Firefox refuses clipboard writes once the click's transient user activation is spent, so the copy could fail silently even on localhost. The clipboard write now starts synchronously in the click handler with a `ClipboardItem` promise for the fetched text, keeping it inside the user activation.

#### Full Chunk Viewer on the Ask Page
- Ask source chunks can now be opened in full: each source's file name is a link that shows the complete stored chunk in the same viewer the search pages use (the modal was extracted into a shared partial). The viewer's title now shows the file name instead of the internal row id on all pages.

#### Keyword Search No-Results Hint
- The keyword-search empty state now explains that matching is on exact word forms (no stemming, so "apple" will not match "apples") and points at Semantic Search for meaning-based matches, instead of only suggesting a shorter or broader query.

#### Empty Full-Text Index Folder
- Checking whether a vault supports keyword search no longer creates an empty `fulltext_vault/` folder inside the vault. Opening or indexing a vault probed for the Whoosh index by constructing it, which created the directory as a side effect — so every vault appeared to have a full-text index on disk while the Keyword Search page correctly reported that none existed. The folder is now only created when the index is actually built.

### Documentation

- The README no longer claims Ask "falls back" to the scripted eliza responder when no chat provider is configured. The default chat setting is Ollama, so out of the box Ask reports a connection error (with guidance) until a provider is reachable; the README now says so and explains that eliza is selected as the chat source on the Settings page to smoke-test without any provider.
- The README's alpha status note now warns that the PyPI release can lag the README and points at the source install for the documented behavior, since `pip install talkpipe-vault` can deliver an older release whose first-run flow differs from the current docs.
- The OpenAI provider notes now document the **OpenAI base URL** field (Settings → Connections & credentials, or `OPENAI_BASE_URL`), which points the provider at any OpenAI-compatible endpoint such as vLLM, LM Studio, or a llama.cpp server. The field existed in the UI but was undocumented.
- The file-watching example now warns that events raised right after the watcher starts can arrive late, and that redirected output needs `python -u` (the `Print()` segment's output is block-buffered off-terminal), so a silent first run isn't mistaken for a broken example.
- The README's Python badge now matches the Requirements section and `pyproject.toml` (3.11.4+).

- The first `vault-server` launch snippet now points readers to the Installation section, so skimmers don't run the command before anything is installed.
- The "Overriding in Code" example is now complete and runnable: it shows that `build_vector_db_from_paths(...)` only constructs a pipeline segment and demonstrates feeding items through it, instead of a bare constructor call that silently indexes nothing.
- The "Building Your Own Pipelines" watcher example now notes where Ollama components get their server URL (with a link to Provider-Specific Configuration) and warns that a single file save typically produces both `created` and `modified` events, so a file can be processed twice in a row (the vector database deduplicates by document id).
- The experimental directory-monitoring section reflects the watcher fix above: each indexed chunk is printed as it is processed, rather than the watcher printing nothing per file.

#### Startup with Stale Saved Settings
- A saved provider choice that is no longer available (for example a plugin-provided embedder whose package was uninstalled) no longer aborts `vault-server` / `python -m talkpipe_vault.apps.query` startup with a raw `Source '...' is not supported` error. The stale embedding or chat override is now dropped with a warning that names the settings file, the server starts with the configured defaults, and the Settings page's configuration status can flag anything still wrong. The same guard applies to the embedder recorded in a vault's `vault_metadata.json`.

#### Ollama Connection Guidance
- When Ask cannot reach the Ollama server, the error now also points at the in-app fix — Settings → Connections & credentials — instead of only suggesting the `TALKPIPE_OLLAMA_SERVER_URL` environment variable or `~/.talkpipe.toml`. The configuration-status hint on the Settings page does the same.

#### API Key Error Guidance
- Missing OpenAI/Anthropic credential errors on the Ask page now also point at the in-app fix — Settings → Connections & credentials — matching the Ollama connection guidance, instead of only suggesting the `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` environment variables.

#### Watcher Path Validation
- `fileWatcher` (`file_watcher`) now fails fast with an error that names a nonexistent watch path. Previously a bad path (for example the README's `/path/to/watch` placeholder) surfaced as a bare `FileNotFoundError: [Errno 2] No such file or directory` from inotify that never said which path was the problem. The watch path also expands `~` now.
- The experimental `watch_vectordb_main` helper validates the watch directory before building its pipeline, so a bad path exits with a clean CLI error instead of writing vault scaffolding to disk and then dying with a filesystem traceback.

#### Opening a Documents Folder as a Vault
- Opening a non-empty folder that contains no vault data (an easy mix-up between a documents folder and a vault folder) now lands on Add Documents with a notice that index files will be created alongside the folder's existing contents, instead of silently starting to write vault scaffolding next to the user's files.

#### Consistent Page Titles
- Browser-tab titles now follow one `<page> - Talkpipe Vault` pattern on every page; the home page previously titled itself "Vault Query - Home" (a leftover internal name) and the search/ask pages used assorted "Vault ..." titles.

#### Offline Markdown Rendering
- The web interface now serves its Markdown renderer (`marked.min.js`) from the app's own static files instead of loading it from the jsDelivr CDN. On firewalled or offline machines (the same deployments the `HF_HUB_OFFLINE` guidance targets), Ask answers previously fell back to unrendered plain text because the CDN request failed.

#### Tilde Vault Paths Split Across Two Locations
- Passing a vault path like `~/my-vault` (as the README's pipeline examples do) no longer splits the vault: LanceDB expanded the `~` itself while the Whoosh index path did not, so the vector tables landed in `$HOME/my-vault` but the full-text index in a literal `./~/my-vault/fulltext_vault` directory. The vault path helpers now expand `~` for both stores.

#### Indexing Console Noise
- The `watchIntoVectorDB` and `listIntoVectorDB` sources no longer print a raw `{'path': ...}` dict to stdout for every file they process (a leftover `Print()` diagnostic stage). The CLI helpers keep their per-chunk progress output.

#### Ask Answers No Longer Cite Absolute Paths
- The RAG prompt now instructs the model to cite the files behind an answer by file name or title only. Previously answers listed absolute server filesystem paths even though source paths are hidden by default (`--show-source-paths` off).

#### Search Highlighting
- Semantic and keyword search results no longer highlight common stopwords or the boolean operators AND/OR/NOT from the query (previously e.g. every "and" in every snippet lit up). Quoted phrases are still highlighted verbatim.

#### Form Submissions
- Submitting any form with an empty field (Open or Create, Search, Keyword Search, Ask, Index Documents) no longer surfaces a raw `422` JSON validation error; empty submissions now land on a friendly page or redirect with a clear message. Inputs that require a value also declare it client-side.

#### Search Result Scores
- Semantic search (and the Ask source chunks) no longer show a misleading "Score: 0.0000" on every result. Vector backends such as the default model2vec report a score of 0.0 with no distance, so the score badge is now hidden when no meaningful similarity is available; keyword search continues to show its Whoosh relevance scores, and any backend that does return a similarity still shows it.

#### Server Console Output
- The Ask/RAG pipeline no longer dumps the full query embedding vector and assembled RAG prompt to the server console on every question (a leftover `diagPrintOutput="stdout"` diagnostic), so the server log stays readable.

#### Keyword Index Creation
- Building the full-text index no longer commits the Whoosh writer once per
  document, which made Whoosh re-merge its segments on every add and degraded
  quadratically with vault size (a 20k-chunk rebuild took ~6.5 minutes; it now
  takes ~14 seconds). The rebuild holds a single writer and commits once at the
  end.
- Fixed "Create Full-Text Index" failing with `Schema() got multiple values for keyword argument 'doc_id'`: TalkPipe 0.12.4 reserves the Whoosh `doc_id` schema field, so the index is now built through `WhooshFullTextIndex` directly, keeping LanceDB row ids as stable document ids so results can be resolved back to stored chunks. Rebuilding replaces the previous index contents, and the success notice reports how many documents were indexed. The regression tests build a real index instead of mocking the builder, which is what had hidden this incompatibility.

#### Ollama Server Configuration
- Replaced `OLLAMA_BASE_URL` with `TALKPIPE_OLLAMA_SERVER_URL` in the README, `.env.example`, and Compose files. TalkPipe reads the Ollama server URL from `TALKPIPE_OLLAMA_SERVER_URL` (or `OLLAMA_SERVER_URL` in `~/.talkpipe.toml`); the previously documented variable had no effect.

#### Packaging Cleanup
- Fixed coverage and isort configuration to reference the `talkpipe_vault` package (previously pointed at a nonexistent `vault` package), removed a stale package-data entry and commented-out console scripts, removed the empty `segments` module, and cleaned an unused import out of the plugin initializer.
- Added a Python 3.14 trove classifier (the package builds and runs on 3.14).

### Documentation

#### PyPI Installation
- The Installation section now documents `pip install talkpipe-vault` from PyPI as the standard install path, keeping the editable source install as the contributor/alternative route.

#### Settings Page Coverage
- Documented the full Settings page: the live Configuration status panel (provider reachability tests with Re-test and fix hints), the chunking/Ask retrieval settings, and the Connections & credentials section for entering API keys, an OpenAI-compatible base URL, and the Ollama server URL in the browser. Noted that these connection values persist to `~/.talkpipe-vault/credentials.json` (owner-only permissions) and apply to the vault process only, and listed the Settings page as a way to point Ollama at a remote server.

#### CLI and Storage Details
- Added the `--no-browser` flag to the documented `vault-server` usage (skips opening a browser; useful headless).
- Documented `vault_metadata.json` in the stable vault layout: it records the embedding source/model the vault was indexed with so reopening the vault restores a matching embedder.
- Clarified that `vault_metadata.json` is written by the Add Documents page: vaults built with `makevectordatabase` alone don't carry it and are treated as legacy vaults (flagged on the Settings page when the current embedder may not match the index). The layout section previously implied both tools produced the file.
- Set expectations for the experimental directory-monitoring helper: it only reacts to filesystem events after it starts (existing files are indexed with the list helper instead) and currently prints nothing per processed file.
- Clarified that "recently used vaults are remembered" means they are listed on the Vaults page at the next start for one-click reopening — the last vault is not reopened automatically.

#### Provider Coverage
- Documented the remaining chat providers selectable in the web interface: Anthropic (key via Settings → Connections & credentials or `ANTHROPIC_API_KEY`) and the built-in keyless `eliza` responder for smoke-testing the Ask page, and listed both in the `chat_source` configuration table row.

#### Developer Tooling
- Added a `.flake8` configuration matching the black/isort style (line length 88, E203/W503 ignored), so the documented `flake8 src/ tests/` command passes on a clean checkout, and removed three unused imports it flagged in the watchdog tests.
- Raised mypy's `python_version` to 3.12 so `mypy src/` can parse modern dependency stubs (numpy's `type` statements previously aborted the run before checking any project file), and marked type checking as advisory in the README to match CI.

#### Building Your Own Pipelines examples
- Fixed the "Complete document processing" example, which crashed on the first file event with `'ExtractionResult' object has no attribute 'strip'`: TalkPipe's `ReadFile` stores an `ExtractionResult` object rather than a plain string, so the example's filter expression now goes through `.content`, and the example notes the object's fields.
- Fixed the "Using registered components via configuration" example, which imported a non-existent `talkpipe.Pipeline` and a fabricated `Pipeline.from_config` API. It now uses the real config-driven path — a chatterlang script compiled with `talkpipe.compile` — and references the registered source by name with its correct parameter (`source_pattern`).
- Flagged the "Building Your Own Pipelines" examples as using the experimental watcher/list components, which write the `full_documents`/`shingled_chunks` layout rather than the `docs` table that `vault-server` reads, and clarified that the file-watcher example runs until interrupted.

#### Installation
- Added an explicit virtual-environment step to the Installation and Development Setup instructions, with a note that recent Linux distributions mark the system Python as externally managed (PEP 668), so a bare `pip install` into it fails. Quoted the `.[dev]` extras so the command also works in shells like zsh.

#### Keyword search behavior
- Documented that web keyword search matches exact, case-insensitive word tokens (no stemming) — e.g. `apple` does not match `apples` — and to prefer semantic search for meaning-based lookups.

#### Vault storage structure
- Rewrote the "Vault Storage Structure" section to lead with the stable layout that `vault-server` actually reads (the `docs` LanceDB table produced by Add Documents / `makevectordatabase`, plus the on-demand `fulltext_vault` Whoosh index) and clearly label the `full_documents`/`shingled_chunks` tables as the separate experimental watcher layout.

#### Container / Podman Deployment
- Removed the Container Deployment section and all container references (Podman/Docker build and run, Compose, `Containerfile`/`docker-compose.yml`/`.env.example` in the project tree, and the "locally or in a container" / "default container" mentions) from the README. The container workflow needs more thorough testing before it is advertised; it will be documented again once verified.
- (Earlier) Added README section for Podman deployment: prerequisites, quick start, configuration, scripts, debugging, Ollama access, troubleshooting; updated container examples and Compose environment to support a remote Ollama server at `deeplearn`.

#### TalkPipe 0.12 Compatibility Notes
- Updated README and `vault-query` CLI help to clarify that query mode expects a LanceDB path containing TalkPipe's `docs` table (for example, output from `makevectordatabase`).

### Bug Fixes

#### Vault Path Semantics
- Standardized `vault_path` semantics across app and pipelines to match TalkPipe `makevectordatabase`: LanceDB is read/written directly at `vault_path`.
- Added strict legacy-layout validation that fails fast when `vault_path/vector_vault` exists, with migration guidance to move LanceDB contents to `vault_path`.
- Removed query-app fallback probing of `vault_path/vector_vault`; reads now use only the canonical LanceDB path.
- Kept Whoosh full-text index path at `vault_path/fulltext_vault`.

#### Query App Whoosh Full-Text Flow
- Fixed query app template rendering with newer Starlette versions by passing
  `TemplateResponse` arguments by keyword.
- Switched `vaultTextSearch` to TalkPipe's `searchWhoosh` segment instead of LanceDB FTS.
- Updated keyword-search enablement to detect a Whoosh index at `fulltext_vault`.
- Added a keyword-search UI action to create a Whoosh index from existing LanceDB `docs` records using TalkPipe's `indexWhoosh` segment.
- Updated keyword-search UI copy and error messages to reference Whoosh indexing behavior.
- Fixed full-text result links so they can resolve content by LanceDB row id and snippet fragments from the middle of a document.
- Added console output showing the full text of each document sent to the Whoosh index.
- Stored LanceDB source paths in the Whoosh index and displayed them as file links in keyword-search results.
- Stopped collapsing keyword-search hits that share a source path and removed the low 10-result search cap.
- Stopped collapsing semantic-search hits that share a source path.
- Updated semantic-search result fields and display text to match keyword-search results.
- Hid source paths by default and added `--show-source-paths` to display HTTP links served by the query app.
- Restricted source-file downloads to paths referenced by the current vault index.
- Updated the app header to show the current chunk count only, removing the full-text index stat.
- Refined the query app UI with a polished header, status badge, navigation, cards, and form styling.
- Added a header refresh action, Ask answer source citations, reusable search result UI, snippet highlighting, per-result copy buttons, and more helpful empty states.
- Added per-chunk copy buttons to Ask source citations and changed keyword-search result copy buttons to copy the full stored chunk rather than only the displayed snippet.
- Switched the query app header logo to the packaged SVG asset and exposed full-text index rebuild from the keyword-search page.
- Fixed `VaultTextSearch` so missing Whoosh indexes return empty results instead of raising.
- Fixed keyword-search result clicks when Whoosh returns flat hits without a `doc_id` or has stale row ids.

#### Query App Uses TalkPipe Native Vector DB
- Updated `vaultSearch` and `vaultChat` to query the `docs` table used by TalkPipe's `makevectordatabase` and `serverag` commands.
- Updated the web query app to treat `vault_path` as the LanceDB path and read document counts from the `docs` table.
- Added a new `searchLance` segment and migrated `vaultTextSearch` from Whoosh to LanceDB-backed keyword search.
- Changed LanceDB keyword search to use only pre-existing FTS indexes; when no FTS index is present, keyword search is disabled without creating or upgrading indexes.
- Disabled the keyword search UI when the LanceDB `docs` table has no FTS index.

#### File Watcher on Linux
- **Move-only handler**: Added a second handler that receives all `FileMovedEvent` events without pattern filtering. `PatternMatchingEventHandler` filters moves when `src_path` is outside the watch tree (e.g., `mv` from `/tmp`, drag-and-drop), causing them to be dropped. The new handler bypasses this and correctly indexes moved files.
- **PollingObserver startup**: Skip sentinel file check when using `--polling`. The check required writing to the watch directory and could fail in container bind-mount setups; PollingObserver does not need inotify verification.

### Refactoring

#### Dependency Cleanup
- Removed `docling` and `tika` references from package metadata, container/test scripts, and project documentation to align with the current text extraction pipeline.

#### App Startup Behavior
- Updated `vault-server` to start only the web interface for search/chat and not launch the file watcher pipeline.

#### Container Tooling
- Removed duplicate `Dockerfile`; kept `Containerfile` only
- Removed Podman wrapper shell scripts in favor of direct `podman`/Compose commands
- Added `docker-compose.yml` with production and development-profile services

#### Container Simplification
- Removed live file watching from default container startup; containers now start the web-only `vault-server`
- Moved internal container data to `/app/data/vault`
- Updated `Containerfile` to define a single volume at `/app/data`
- Removed the shell entrypoint; `Containerfile` now starts `vault-server` directly

#### `building_and_watching` Simplifications
- Removed unused `extract_property` import
- Added `resolve_embedding_config()` and `get_vault_paths()` helpers in config module
- Unified `watch_into_vector_db` pipeline construction (single branch with conditional Debounce)
- Extracted `_non_empty_filter()` helper using lambdaFilter (FilterExpression) for content/chunk/shingle
- Split `build_vector_db_from_paths` pipeline into named `full_doc_stage` and `shingle_stage` for readability

### New Features

#### `fileWatcher` Source
A TalkPipe source that watches directories for file system changes.

- Monitors directories recursively for file creation, modification, and deletion events
- Emits event dictionaries with `event` type ("created", "modified", "deleted") and `path`
- **Pattern filtering:** Include only specific files with `patterns` parameter (e.g., `["*.txt", "*.md"]`)
- **Ignore patterns:** Exclude files with `ignore_patterns` parameter
- **Common file ignoring:** `ignore_common` parameter (default: True) automatically ignores:
  - Hidden files (`.*`)
  - Editor temp/backup files (`*~`, `#*#`, `*.swp`, `*.swo`, `.#*`)
  - Temp files (`*.tmp`, `*.temp`, `*.bak`)
  - Office temp files (`~$*`)
  - Python cache (`*.pyc`, `__pycache__`)
- **Polling mode:** `polling` parameter for network filesystems where native events are unreliable
- **Event limiting:** `max_events` parameter to stop after processing a set number of events
- **Case sensitivity:** Configurable pattern matching with `case_sensitive` parameter

#### `doclingToText` Segment
A TalkPipe segment that extracts text content from documents using the Docling library.

- Converts PDF, DOCX, PPTX, HTML, and other formats to markdown text
- Native support for plain text (`.txt`) files without Docling overhead
- Graceful error handling: logs warnings and skips files that fail to convert
- Works as a field segment with `field` and `set_as` parameters

### `pipelines` Package
A high-level package that creates application components by composing sources and segments for higher-ordered problems.

##### `building_and_watching` Pipelines

**`buildVectorDBFromPaths` Segment**
A TalkPipe segment that builds a vector database from document file paths.

- Extracts text content from documents using Docling
- Creates dual-table vector database structure:
  - `full_documents` table: Stores complete document embeddings
  - `shingled_chunks` table: Stores overlapping chunk embeddings for fine-grained retrieval
- Automatic text chunking (500 character segments) with 3-shingle overlap
- Supports LanceDB with file paths, `memory://`, or `tmp://name` storage options
- Configurable embedding model and source

**`watchIntoVectorDB` Source**
A TalkPipe source that monitors a directory and automatically indexes new/modified documents into a vector database.

- Combines `fileWatcher` source with `buildVectorDBFromPaths` segment
- Real-time document indexing as files are created or modified
- Inherits all `fileWatcher` filtering capabilities (patterns, ignore patterns, polling mode)
- Configurable embedding model and vector database destination
