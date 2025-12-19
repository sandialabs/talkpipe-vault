<p align="center">
  <img src="docs/talkpipe_vault.jpg" alt="TalkPipe Vault Logo" width="300">
</p>

# TalkPipe Vault

> AI-powered personal information assistant that watches your documents and makes them searchable

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Development Status](https://img.shields.io/badge/status-alpha-yellow.svg)](https://github.com/sandialabs/talkpipe-vault)

## What is TalkPipe Vault?

**TalkPipe Vault** is a set of practical tools and reusable components for turning folders of files into a searchable “vault” you can explore with semantic search, keyword search, and retrieval‑augmented Q&A. It is a production‑oriented example built on the **[TalkPipe](https://github.com/sandialabs/talkpipe)** framework, demonstrating how to assemble document processing, vector search, and RAG with clean, composable pipelines.

What you get:
- **CLI applications for indexing**: `vault-watch-into-vectordb` (watch a directory and index changes) and `vault-list-into-vectordb` (bulk index from a path or glob).
- **A web application for discovery**: `vault-query` starts a FastAPI UI for semantic search, keyword search, and single‑turn Q&A over your vault.
- **Reusable building blocks**: TalkPipe sources, segments, and end‑to‑end pipelines that you can compose to build your own file/document management workflows.

How it works (at a glance):
- Watches directories or enumerates files, converts documents to text (Docling), and stores full‑document and shingled‑chunk embeddings in LanceDB.
- Builds a Whoosh full‑text index for precise keyword queries alongside semantic search.
- Supports local models (Ollama) and cloud providers (OpenAI) via simple configuration.

Together, these applications and components provide both ready‑to‑use capabilities and a clear blueprint for creating custom pipelines with TalkPipe.

### Key Features

- **Automatic Document Monitoring**: Watches folders and automatically processes new or modified documents
- **Semantic Search**: Find documents by meaning, not just keywords
- **Multiple AI Backends**: Works with OpenAI or local Ollama models (privacy-friendly, no cloud required)
- **Format Support**: Handles various document formats via [Docling](https://github.com/DS4SD/docling)
- **Vector Database**: Uses [LanceDB](https://lancedb.com/) for efficient similarity search
- **Intelligent Chunking**: Breaks documents into overlapping chunks for better search accuracy

### Web Interface

TalkPipe Vault includes a web application for searching and querying your document collection:

- **Semantic Search**: Find documents by meaning using AI-powered vector similarity search
- **Keyword Search**: Traditional full-text search with boolean operators (AND, OR, NOT) and phrase matching
- **Ask a Question**: Get AI-generated answers based on your vault's contents (single-turn Q&A)
- **Copy Results**: Easily copy search results or answers to clipboard

**Launch the web interface:**

```bash
vault-query ~/my-vault --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000 in your browser.

## Quick Start

### Installation

```bash
# Install from source
git clone https://github.com/sandialabs/talkpipe-vault.git
cd talkpipe-vault
pip install -e .

# For development
pip install -e .[dev]
```

### Basic Usage

**Watch a folder and build a searchable database:**

```bash
# Watch a directory and index matching files
vault-watch-into-vectordb "/path/to/documents" \
    --vault-path ~/my-vault \
    --patterns "*.txt" "*.md" \
    --ignore-patterns "*/node_modules/*" \
    --polling \
    --overwrite
```

**Index an existing collection:**

```bash
vault-list-into-vectordb "/path/to/documents/**/*.txt" \
    --vault-path ~/my-vault \
    --overwrite
```

The commands above populate a vault at `~/my-vault` with:
- `vector_vault/full_documents`: Embeddings for full documents (broad search)
- `vector_vault/shingled_chunks`: Embeddings for shingled text windows (precise retrieval)
- `fulltext_vault`: Whoosh full‑text index for keyword search

---

## For Developers

### Architecture

TalkPipe Vault is built on [TalkPipe](https://github.com/sandialabs/talkpipe), a Python framework for composable data pipelines. The project provides custom TalkPipe sources and segments for document processing and vector database creation.

**Technology Stack:**

- **Pipeline Framework**: TalkPipe for composable data processing
- **Document Processing**: Docling for multi-format document conversion
- **Vector Database**: LanceDB for semantic search
- **Full-Text Search**: Whoosh for keyword-based search
- **File Monitoring**: Watchdog for filesystem events
- **Web Framework**: FastAPI with Jinja2 templates
- **AI/Embeddings**: OpenAI API or Ollama (local inference)

### Processing Pipeline

The document processing pipeline consists of several stages:

```
File Event → Document Parsing → Filtering → Full-Doc Embedding →
Text Chunking → Shingle Generation → Chunk Embedding → Vector Storage
```

1. **File Watcher**: Monitors directories for file system events (create, modify, delete)
2. **Document Parsing**: Converts various formats to text using Docling
3. **Filtering**: Removes empty or deleted documents
4. **Full Document Embedding**: Stores complete documents in `full_documents` table
5. **Text Chunking**: Splits documents into ~500 character chunks
6. **Shingle Generation**: Creates overlapping 3-chunk windows for context preservation
7. **Chunk Embedding**: Stores shingled chunks in `shingled_chunks` table

### Command-Line Tools

TalkPipe Vault provides several CLI commands:

#### `vault-watch-into-vectordb`

Monitors a directory and automatically processes documents as they're created or modified.

```bash
vault-watch-into-vectordb [SOURCE_PATH] [OPTIONS]
```

**Options:**
- `--vault-path TEXT`: Base path for vault storage. Vector DB at `vault_path/vector_vault`, full‑text index at `vault_path/fulltext_vault` (required)
- `--patterns PATTERN [PATTERN ...]`: Glob patterns to include (e.g., `"*.txt" "*.md"`)
- `--ignore-patterns PATTERN [PATTERN ...]`: Glob patterns to exclude
- `--include-directories`: Include directory events (default: ignore directories)
- `--case-sensitive`: Case‑sensitive pattern matching
- `--max-events INT`: Maximum number of events to process
- `--polling`: Use polling observer (fallback when native events unavailable)
- `--include-common`: Include common temp/hidden files (default: ignore)
- `--overwrite`: Overwrite existing tables and indexes
- `--delete-after-reading`: Delete source files after successful indexing
- `--debounce-seconds FLOAT`: Wait for file stability before processing (default: 1.0; 0 to disable)

**Example:**

```bash
vault-watch-into-vectordb ~/Documents \
    --vault-path ~/vault-db \
    --patterns "*.txt" "*.md" \
    --ignore-patterns "*/node_modules/*" \
    --polling \
    --overwrite
```

#### `vault-list-into-vectordb`

Processes an existing collection of files matching a path or glob pattern.

```bash
vault-list-into-vectordb [SOURCE_PATTERN] [OPTIONS]
```

**Options:**
- `--vault-path TEXT`: Base path for vault storage (required)
- `--overwrite`: Overwrite existing tables and indexes
- `--delete-after-reading`: Delete source files after successful indexing

**Example:**

```bash
vault-list-into-vectordb "~/Documents/**/*.pdf" \
    --vault-path ~/vault-db \
    --overwrite
```

#### `vault-query`

Launches the web interface for searching and querying your vault.

```bash
vault-query [VAULT_PATH] [OPTIONS]
```

**Options:**
- `--host TEXT`: Host to bind to (default: `127.0.0.1`)
- `--port INT`: Port to listen on (default: `8000`)

**Example:**

```bash
vault-query ~/my-vault --host 0.0.0.0 --port 8080
```

The web interface provides three modes:
- **Semantic Search**: Vector similarity search to find documents by meaning
- **Keyword Search**: Full-text search with Whoosh query syntax (AND, OR, NOT, phrases)
- **Ask**: Single-turn Q&A that retrieves relevant context and generates AI responses

### Custom TalkPipe Components

TalkPipe Vault registers custom sources and segments with TalkPipe:

**Sources:**
- `fileWatcher`: File system event monitoring
- `watchIntoVectorDB`: Combined watching and vector database creation
- `listIntoVectorDB`: Batch processing from glob patterns

**Segments:**
- `doclingToText`: Document format conversion
- `buildVectorDBFromPaths`: Complete document processing pipeline
- `vaultSearch`: Semantic search on vault's vector database
- `vaultTextSearch`: Full-text keyword search using Whoosh index
- `vaultChat`: RAG-based Q&A using vault contents

### Building Your Own Pipelines

One of TalkPipe's strengths is composability. Here's how TalkPipe Vault builds complex functionality from simple pipeline operators:

**Example 1: Simple file watching pipeline**

```python
from talkpipe_vault.watchdog import file_watcher
from talkpipe.pipe.io import Print

# Watch a directory and print events
pipeline = file_watcher(path="/path/to/watch") | Print()

# Run the pipeline
for event in pipeline():
    # Process events as they occur
    pass
```

**Example 2: Complete document processing (from TalkPipe Vault source)**

```python
from talkpipe_vault.watchdog import file_watcher
from talkpipe_vault.docling import DoclingFileToText
from talkpipe.pipe.basic import FilterExpression
from talkpipe.pipelines.vector_databases import MakeVectorDatabaseSegment

# Build a complete document intelligence pipeline by chaining components
pipeline = \
    file_watcher(path="/path/to/watch") | \
    FilterExpression(expression="item['event'] != 'deleted'") | \
    DoclingFileToText(field="path", set_as="full_content") | \
    FilterExpression(expression="len(item.get('full_content', '').strip()) > 0") | \
    MakeVectorDatabaseSegment(
        path="~/my-vault",
        embedding_model="mxbai-embed-large:latest",
        embedding_source="ollama",
        embedding_field="full_content",
        table_name="documents",
        doc_id_field="path"
    )

# Run the pipeline
for result in pipeline():
    print(f"Processed: {result['path']}")
```

**Example 3: Using registered components via configuration**

```python
from talkpipe import Pipeline

# Create a pipeline from configuration
pipeline = Pipeline.from_config({
    "source": {
        "type": "watchIntoVectorDB",
        "source_path": "/path/to/watch",
        "vectordb_path": "~/my-vault",
        "embedding_model": "mxbai-embed-large:latest",
        "embedding_source": "ollama",
        "polling": True
    }
})

# Run it
list(pipeline())
```

This composability is what makes TalkPipe powerful—you can build sophisticated AI applications by connecting well-tested components like Lego blocks.

### Vault Storage Structure

The vault at `vault_path` contains:

- `vector_vault/full_documents`: Embeddings for templated full‑document content (unique id is document‑based)
- `vector_vault/shingled_chunks`: Embeddings for overlapping chunk windows with composite ids like `first-last-source`
- `fulltext_vault`: Whoosh full‑text index over full document content

These are produced by the pipelines in [src/talkpipe_vault/pipelines/building_and_watching.py](src/talkpipe_vault/pipelines/building_and_watching.py).

### Development Setup

```bash
# Clone repository
git clone https://github.com/sandialabs/talkpipe-vault.git
cd talkpipe-vault

# Install with development dependencies
pip install -e .[dev]

# Run tests
pytest

# Run tests with coverage
pytest --cov=src --cov-report=term-missing --cov-report=html

# Code formatting
black src/ tests/
isort src/ tests/

# Linting
flake8 src/ tests/

# Type checking
mypy src/

# Security scanning
bandit -r src/
safety check
```

### Model Configuration & Environment

- Models are configured in [src/talkpipe_vault/pipelines/config.py](src/talkpipe_vault/pipelines/config.py):
    - **Embeddings:** `EMBEDDING_MODEL` (default: `embeddinggemma`), `EMBEDDING_SOURCE` (default: `ollama`)
    - **Chat:** `CHAT_MODEL` (default: `mistral-small`), `CHAT_SOURCE` (default: `ollama`)
- To use OpenAI instead, set sources to `"openai"` in the config and ensure:
    - `OPENAI_API_KEY` is set in your environment
- For Ollama, you can customize the server with:
    - `OLLAMA_BASE_URL` (default: `http://localhost:11434`)

### Project Structure

```
talkpipe-vault/
├── src/
│   └── talkpipe_vault/
│       ├── pipelines/
│       │   ├── building_and_watching.py    # Core pipeline logic
│       │   ├── searching_and_prompting.py  # Search and RAG segments
│       │   ├── config.py                   # Default configuration
│       │   └── cli.py                      # CLI entry points
│       ├── apps/
│       │   ├── query.py                    # Web application
│       │   └── templates/                  # HTML templates
│       ├── watchdog.py                     # File system monitoring
│       └── docling.py                      # Document conversion
├── docs/
│   └── talkpipe_vault.jpg                  # Project logo
├── tests/                                  # Test suite
├── pyproject.toml                          # Package configuration
├── Dockerfile                              # Container image
└── docker-compose.yml                      # Service orchestration
```

## Requirements

- **Python**: 3.11.4 or higher
- **Ollama** (optional): For local embedding models
- **OpenAI API Key** (optional): For cloud-based embeddings

## Contributing

Contributions are welcome! Please ensure:

1. Tests pass: `pytest`
2. Code is formatted: `black src/ tests/ && isort src/ tests/`
3. Linting passes: `flake8 src/ tests/`
4. Type hints are complete: `mypy src/`

## License

Apache License 2.0 - See [LICENSE](LICENSE) file for details.

## Authors

- **Travis Bauer** - *Initial development* - [Sandia National Laboratories](https://www.sandia.gov/)

## Acknowledgments

- Built with [TalkPipe](https://github.com/sandialabs/talkpipe)
- Document processing via [Docling](https://github.com/DS4SD/docling)
- Vector storage using [LanceDB](https://lancedb.com/)
- File monitoring with [Watchdog](https://github.com/gorakhargosh/watchdog)

---

**Status**: Alpha - Active development. APIs may change.
