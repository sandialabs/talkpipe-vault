# Changelog

## In Development

### Documentation

#### Podman Deployment
- Added README section for Podman deployment: prerequisites, quick start, configuration, scripts, debugging, Ollama access, troubleshooting

#### TalkPipe 0.12 Compatibility Notes
- Updated README and `vault-query` CLI help to clarify that query mode expects a LanceDB path containing TalkPipe's `docs` table (for example, output from `makevectordatabase`).

### Bug Fixes

#### Query App Whoosh Full-Text Flow
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
- Fixed `VaultTextSearch` so missing Whoosh indexes return empty results instead of raising.

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

#### Podman Scripts
- Removed duplicate `Dockerfile`; kept `Containerfile` only
- Simplified `podman-build.sh` (removed Containerfile/Dockerfile branching)
- `podman-run.sh` delegates to `podman-build.sh` when image is missing
- Added `podman-config.sh` for shared IMAGE_NAME, VAULT_DIR, WATCH_DIR, DESKTOP_DIR
- Simplified `podman-shell.sh` flag handling with `case` statement
- Baked `talkpipe_tests.sh` into container image for faster shell connects
- Refactored `entrypoint.sh` with `check_writable()` and `cleanup_lock_files()` helpers

#### Container Simplification
- Moved internal container data directories to `/app/data/vault` and `/app/data/watch`
- Updated `Containerfile` to define a single volume at `/app/data`
- Simplified `entrypoint.sh`: removed complex permission repair logic in favor of simple writability check
- Updated `podman-run.sh` to mount a single data directory to `/app/data`

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
