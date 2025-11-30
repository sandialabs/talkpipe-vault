# TalkPipe Vault

> AI-powered personal information assistant that watches your documents and makes them searchable

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Development Status](https://img.shields.io/badge/status-alpha-yellow.svg)](https://github.com/sandialabs/talkpipe-vault)

## What is TalkPipe Vault?

**TalkPipe Vault** helps you find information in your personal documents using natural language and AI. Instead of remembering exactly where you saved something or what you named a file, you can search using concepts and ideas.

Imagine having thousands of documents, notes, PDFs, and text files scattered across your computer. TalkPipe Vault watches these folders, reads your documents, and creates an intelligent index that understands the *meaning* of your content—not just keywords. This means you can ask questions like "notes about machine learning from last month" or find related documents even if they use different terminology.

### Built with TalkPipe

TalkPipe Vault is built on **[TalkPipe](https://github.com/sandialabs/talkpipe)**, a Python framework for creating composable AI data pipelines. TalkPipe Vault demonstrates what you can build with TalkPipe—a complete document intelligence system created by connecting simple, reusable pipeline components.

**TalkPipe Vault serves as a real-world example** of how TalkPipe enables you to:
- Chain together document processing, AI models, and databases without boilerplate
- Create reusable pipeline components that can be mixed and matched
- Build production-ready AI applications with minimal code
- Extend existing pipelines with custom functionality

If you're interested in building your own AI-powered data processing tools, TalkPipe Vault showcases the framework's capabilities for real-world applications.

### Key Features

- **Automatic Document Monitoring**: Watches folders and automatically processes new or modified documents
- **Semantic Search**: Find documents by meaning, not just keywords
- **Multiple AI Backends**: Works with OpenAI or local Ollama models (privacy-friendly, no cloud required)
- **Format Support**: Handles various document formats via [Docling](https://github.com/DS4SD/docling)
- **Vector Database**: Uses [LanceDB](https://lancedb.com/) for efficient similarity search
- **Intelligent Chunking**: Breaks documents into overlapping chunks for better search accuracy

### Coming Soon: Web Interface

A web application for searching and chatting with your document collection is planned for future releases. The web interface will provide:

- **Interactive Search**: Natural language queries with instant results
- **Document Chat**: Ask questions about your documents and get AI-generated answers with citations
- **Visual Timeline**: Browse documents by time and topic
- **Collection Management**: Organize and tag your document collections

*The backend infrastructure is being developed first, with the web UI to follow.*

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
# Using Ollama (local, privacy-friendly)
vault-watch-into-vectordb "/path/to/documents" \
    --vectordb-path ~/my-vault \
    --embedding-model mxbai-embed-large:latest \
    --embedding-source ollama \
    --polling

# Using OpenAI (requires OPENAI_API_KEY environment variable)
vault-watch-into-vectordb "/path/to/documents" \
    --vectordb-path ~/my-vault \
    --embedding-model text-embedding-3-small \
    --embedding-source openai
```

**Index an existing collection:**

```bash
vault-list-into-vectordb "/path/to/documents/**/*.txt" \
    --vectordb-path ~/my-vault \
    --embedding-model mxbai-embed-large:latest \
    --embedding-source ollama \
    --overwrite
```

The commands above create two searchable tables in your vector database:
- `full_documents`: Complete documents for broad searches
- `shingled_chunks`: Overlapping text segments for precise retrieval

---

## For Developers

### Architecture

TalkPipe Vault is built on [TalkPipe](https://github.com/sandialabs/talkpipe), a Python framework for composable data pipelines. The project provides custom TalkPipe sources and segments for document processing and vector database creation.

**Technology Stack:**

- **Pipeline Framework**: TalkPipe for composable data processing
- **Document Processing**: Docling for multi-format document conversion
- **Vector Database**: LanceDB for semantic search
- **File Monitoring**: Watchdog for filesystem events
- **Web Framework** (planned): FastAPI with SQLAlchemy
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
vault-watch-into-vectordb [PATH] [OPTIONS]
```

**Options:**
- `--vectordb-path TEXT`: LanceDB database path (supports `file://`, `memory://`, `tmp://`)
- `--embedding-model TEXT`: Model name (e.g., `mxbai-embed-large:latest`, `text-embedding-3-small`)
- `--embedding-source TEXT`: `ollama` or `openai`
- `--patterns TEXT`: Glob patterns to include (can specify multiple)
- `--ignore-patterns TEXT`: Glob patterns to exclude
- `--polling`: Use polling observer instead of native file system events
- `--overwrite`: Overwrite existing tables (use on first run)

**Example:**

```bash
vault-watch-into-vectordb ~/Documents \
    --vectordb-path ~/vault-db \
    --embedding-model mxbai-embed-large:latest \
    --embedding-source ollama \
    --patterns "*.txt" --patterns "*.md" \
    --ignore-patterns "*/node_modules/*" \
    --polling
```

#### `vault-list-into-vectordb`

Processes an existing collection of files matching a glob pattern.

```bash
vault-list-into-vectordb [PATTERN] [OPTIONS]
```

**Example:**

```bash
vault-list-into-vectordb "~/Documents/**/*.pdf" \
    --vectordb-path ~/vault-db \
    --embedding-model text-embedding-3-small \
    --embedding-source openai \
    --overwrite
```

### Custom TalkPipe Components

TalkPipe Vault registers custom sources and segments with TalkPipe:

**Sources:**
- `fileWatcher`: File system event monitoring
- `watchIntoVectorDB`: Combined watching and vector database creation
- `listIntoVectorDB`: Batch processing from glob patterns

**Segments:**
- `doclingToText`: Document format conversion
- `buildVectorDBFromPaths`: Complete document processing pipeline

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

### Vector Database Structure

LanceDB tables created by TalkPipe Vault:

**`full_documents` table:**
- `path`: Document file path (unique ID)
- `full_content`: Complete document text
- `vector`: Embedding of full document

**`shingled_chunks` table:**
- `id`: Composite ID (`{first_para}-{last_para}-{path}`)
- `path`: Source document path
- `shingle`: Overlapping text window
- `shingle_detail`: Metadata (paragraph indices, context)
- `vector`: Embedding of shingled chunk

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

### Environment Variables

- `OPENAI_API_KEY`: Required when using `--embedding-source openai`
- `OLLAMA_BASE_URL`: Ollama server URL (default: `http://localhost:11434`)
- `VAULT_SECRET`: JWT signing key for web application (future use)
- `VAULT_HOST`: Web server bind address (future use, default: `0.0.0.0`)
- `VAULT_PORT`: Web server port (future use, default: `8001`)
- `VAULT_DB_PATH`: SQLite database path for web app (future use)

### Project Structure

```
talkpipe-vault/
├── src/
│   └── talkpipe_vault/
│       ├── pipelines/
│       │   ├── building_and_watching.py  # Core pipeline logic
│       │   └── cli.py                    # CLI entry points
│       ├── watchdog.py                   # File system monitoring
│       └── docling.py                    # Document conversion
├── tests/                                # Test suite
├── pyproject.toml                        # Package configuration
├── Dockerfile                            # Container image
└── docker-compose.yml                    # Service orchestration
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
