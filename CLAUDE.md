# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding conventions
- When commenting segments or sources, do not include the parameters in the coode comment.
  Include a description of the data structure that the it expects.  
- For sources and segments, parameters should be defined using the Annotated typing convention.

## Project Overview

TalkPipe Vault is an AI-powered personal information assistant that automatically watches documents, processes them with AI models, and creates a searchable vector database. It demonstrates real-world usage of the TalkPipe framework for composable AI data pipelines.

- **Package**: `talkpipe-vault`
- **Module**: `talkpipe_vault`
- **Python**: 3.11.4+ required
- **Status**: Alpha (active development)
- **License**: Apache 2.0

## Source Code Structure

```
src/talkpipe_vault/
├── __init__.py                 # Package initialization
├── docling.py                  # Document conversion (50+ formats)
├── watchdog.py                 # File system monitoring
└── pipelines/
    ├── config.py               # Default embedding/template config
    ├── cli.py                  # CLI entry points
    ├── building_and_watching.py # Vector DB pipeline definitions
    └── searching_and_prompting.py # Search and RAG chat segments
```

## Core Components

### TalkPipe Sources (registered entry points)
- `fileWatcher` - Real-time file system event monitoring
- `watchIntoVectorDB` - Watch directory and process into vector DB
- `listIntoVectorDB` - Batch process files matching glob pattern

### TalkPipe Segments (registered entry points)
- `doclingToText` - Extract text from 50+ file formats
- `buildVectorDBFromPaths` - Core document processing pipeline
- `vaultSearch` - Semantic search in vector database
- `vaultChat` - RAG-based conversational AI

### CLI Commands
```bash
# Watch directory and build vector DB in real-time
vault-watch-into-vectordb /path/to/docs --vectordb-path ~/my-vault \
    --embedding-model mxbai-embed-large:latest --embedding-source ollama

# Batch process files into vector DB
vault-list-into-vectordb "/path/to/docs/**/*.pdf" --vectordb-path ~/my-vault
```

## Development Commands

### Environment Setup

```bash
# Install development dependencies
pip install -e .[dev]

# Install test dependencies only
pip install -e .[test]

# Install security scanning tools
pip install -e .[security]
```

### Testing

```bash
# Run all tests with coverage
pytest

# Run tests with detailed coverage and reports
pytest --cov=src --cov-report=term-missing --cov-report=html --cov-report=xml

# Run tests with debug logging
pytest --log-cli-level=DEBUG
```

### Code Quality

```bash
# Format code
black src/ tests/

# Check formatting
black --check src/ tests/

# Sort imports
isort src/ tests/

# Lint - errors only
flake8 src/ tests/ --count --select=E9,F63,F7,F82 --show-source --statistics

# Lint - with complexity check
flake8 src/ tests/ --count --exit-zero --max-complexity=10 --max-line-length=88 --statistics

# Type checking (may fail - strict mode enabled)
mypy src/
```

### Security

```bash
# Security analysis
bandit -r src/

# Dependency vulnerability scan
safety check
```

### Docker

```bash
# Production service
docker-compose up -d vault
docker-compose logs -f vault

# Development service (hot reload)
docker-compose --profile dev up vault-dev

# Stop services
docker-compose down
```

## Architecture

### Technology Stack

- **Pipeline Framework**: TalkPipe (>=0.10.2a1) with OpenAI/Ollama support
- **Vector Database**: LanceDB
- **Document Conversion**: Docling (50+ formats)
- **File Monitoring**: Watchdog
- **Web Framework**: FastAPI + Uvicorn (infrastructure exists, not yet used)

### Document Processing Pipeline

```
File Events/Paths
    ↓
DoclingFileToText (extract content from PDF, DOCX, source code, etc.)
    ↓
Template formatting
    ↓
MakeVectorDatabaseSegment (full_documents table)
    ↓
splitText (~500 char chunks)
    ↓
ShingleText (3-chunk overlapping windows)
    ↓
MakeVectorDatabaseSegment (shingled_chunks table)
```

### Vector Database Tables
- `full_documents` - Complete documents with embeddings
- `shingled_chunks` - Overlapping text windows for better retrieval

### Docker Multi-Stage Build

1. **builder stage**: Full build environment (Fedora + dev tools)
   - Runs tests (allowed to fail)
   - Builds wheel package
   - Non-root `builder` user

2. **runtime stage**: Minimal runtime (Fedora + Python)
   - Installs pre-built wheel
   - Non-root `app` user (UID 1001)
   - NUMBA_CACHE_DIR configured for TalkPipe dependencies

### Container Networking

Both services map `host.containers.internal:host-gateway` to access host services (e.g., local Ollama instance).

## Code Standards

- **Line length**: 88 characters (Black)
- **Type hints**: Required (strict mypy enabled)
- **First-party imports**: `talkpipe_vault`
- **Coverage**: Track with pytest-cov
- **Security**: Bandit + Safety scans in CI/CD
- **Code-to-test ratio**: ~1:2.5

## CI/CD Pipeline

GitHub Actions workflow on push/PR to main/master/develop:

1. **Test** (Python 3.11, 3.12, 3.13):
   - flake8, black, isort
   - mypy (allowed to fail)
   - pytest with coverage
   - Codecov upload

2. **Security Scan**:
   - Bandit
   - Safety

3. **Build Container**:
   - Docker build/push to GHCR
   - Trivy security scan

4. **CodeQL Analysis**

5. **Publish** (on release): PyPI upload

## Key Configuration Files

- `pyproject.toml`: Package config, dependencies, tool settings, entry points
- `docker-compose.yml`: Service definitions (vault, vault-dev)
- `Dockerfile`: Multi-stage build
- `.github/workflows/ci-cd.yml`: CI/CD pipeline

## Default Configuration

Located in `src/talkpipe_vault/pipelines/config.py`:
- `EMBEDDING_MODEL`: embeddinggemma
- `EMBEDDING_SOURCE`: ollama
- Templates for document, shingle, and retrieval formatting
