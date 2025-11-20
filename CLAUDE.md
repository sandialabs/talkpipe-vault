# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TalkPipe Vault is an AI-powered personal information assistant. The project is in early development with build infrastructure established but source code pending implementation.

- **Package**: `talkpipe-vault`
- **Module**: `vault`
- **Python**: 3.11.4+ required
- **Port**: 8001

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

# Admin operations
docker-compose exec -it vault vault-create-superuser
docker-compose exec vault vault-admin list

# Stop services
docker-compose down
```

## Architecture

### Technology Stack

- **Framework**: FastAPI + Uvicorn
- **Database**: SQLite with SQLAlchemy (async via aiosqlite)
- **Auth**: fastapi-users with SQLAlchemy backend
- **Templates**: Jinja2
- **AI**: TalkPipe (>=0.10.0) with OpenAI/Ollama support
- **Migrations**: Alembic

### Expected Structure

The source code needs to be implemented under `src/vault/`:

```
src/vault/
├── __init__.py
├── app/
│   ├── main.py          # FastAPI app (uvicorn entry point)
│   ├── server.py        # Server module with CLI
│   ├── templates/       # Jinja2 templates
│   └── static/          # CSS, JS, JSON files
├── create_superuser.py  # Admin creation tool
└── admin_users.py       # User management CLI
```

**Key entry points:**
- Production: `python -m vault.app.server` (Dockerfile line 107)
- Development: `uvicorn src.vault.app.main:app --reload` (docker-compose line 57)

### Environment Variables

**Required:**
- `VAULT_SECRET`: JWT signing key (never use default in production)

**Optional:**
- `VAULT_HOST`: Bind address (default: 0.0.0.0)
- `VAULT_PORT`: Port (default: 8001)
- `VAULT_DB_PATH`: Database path (default: /app/data/vault.db)
- `OPENAI_API_KEY`: OpenAI API key
- `OLLAMA_BASE_URL`: Ollama endpoint (default: http://localhost:11434)

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

### Data Persistence

Docker volumes:
- Production: `vault_db` → `/app/data/vault.db`
- Development: `vault_dev_db` → `/app/data/vault.db`

Backup/restore commands in DOCKER_DEPLOYMENT.md.

## Code Standards

- **Line length**: 88 characters (Black)
- **Type hints**: Required (strict mypy enabled)
- **First-party imports**: `vault`
- **Coverage**: Track with pytest-cov
- **Security**: Bandit + Safety scans in CI/CD

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

- `pyproject.toml`: Package config, dependencies, tool settings
- `docker-compose.yml`: Service definitions (vault, vault-dev)
- `Dockerfile`: Multi-stage build
- `DOCKER_DEPLOYMENT.md`: Deployment guide with admin commands
- `.env.podman.NOCOMMIT`: Local env file (gitignored)
