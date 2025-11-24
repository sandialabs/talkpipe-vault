# Changelog

## In Development

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

