# Docker & Podman Deployment

This guide explains how to deploy TalkPipe Vault using Docker or Podman with a two-step workflow: build the image once, then run it anywhere with environment configuration.

## Overview

TalkPipe Vault runs as a single container that provides:
- **File watcher**: Automatically indexes documents from a watch directory
- **Web application**: Search and query interface on port 8002 (configurable)

The container mounts two directories:
- **Watch directory**: Where you place documents to be indexed
- **Vault directory**: Where LanceDB and Whoosh indices are stored (persistent)

## Two-Step Workflow

### Step 1: Build the Image

In the TalkPipe Vault codebase directory:

```bash
# Clone the repository (first time only)
git clone https://github.com/sandialabs/talkpipe-vault.git
cd talkpipe-vault

# Build the image with Docker
docker build -t talkpipe-vault:latest .

# Or with Podman
podman build -t talkpipe-vault:latest .

# Optional: tag for experimentation
podman build -t talkpipe-vault:experimental .
```

### Step 2: Run with Environment Configuration

Create a deployment directory anywhere on your system:

```bash
# Create deployment directory
mkdir -p ~/my-vault-deployment
cd ~/my-vault-deployment

# Create subdirectories for data
mkdir -p watch vault
```

Create a `.env` file in this directory:

```bash
# .env file
# Container paths (these are fixed - /watch and /vault inside the container)
VAULT_WATCH_DIR=/watch
VAULT_PATH=/vault
VAULT_HOST=0.0.0.0
VAULT_PORT=8002

# Optional: AI provider configuration
OPENAI_API_KEY=sk-your-key-here
# OLLAMA_BASE_URL=http://host.containers.internal:11434
```

**Important**: The `VAULT_WATCH_DIR` and `VAULT_PATH` in the `.env` file should be the container paths (`/watch` and `/vault`), not the host paths. The host paths are specified in the volume mounts when running the container.

Run the container with Docker:

```bash
docker run -d \
  --name talkpipe-vault \
  --env-file .env \
  -p 8002:8002 \
  -v "$(pwd)/watch:/watch" \
  -v "$(pwd)/vault:/vault" \
  talkpipe-vault:latest

# View logs
docker logs -f talkpipe-vault
```

Or with Podman:

```bash
podman run -d \
  --name talkpipe-vault \
  --userns=keep-id \
  --env-file .env \
  -p 8002:8002 \
  -v "$(pwd)/watch:/watch:Z" \
  -v "$(pwd)/vault:/vault:Z" \
  talkpipe-vault:latest

# View logs
podman logs -f talkpipe-vault
```

**Note**: 
- The `:Z` suffix on Podman volumes sets the correct SELinux context for container access.
- The `--userns=keep-id` flag maps your host user's UID/GID into the container, allowing it to access directories you own on the host.

Access the web interface at: `http://localhost:8002`

## Quick Start with Docker Compose

For development or simpler deployments, use docker-compose in the codebase directory:

```bash
# Clone and enter repository
git clone https://github.com/sandialabs/talkpipe-vault.git
cd talkpipe-vault

# Create .env file
cp .env.example .env
# Edit .env with your paths and settings

# Start services
docker-compose up -d vault

# Or with Podman
podman compose up -d vault

# Check logs
docker-compose logs -f vault
```

This will use the `.env` file to configure watch and vault directories relative to the project.

## Configuration

### Environment Variables

The container is configured via environment variables, typically set in a `.env` file:

| Variable | Description | Default |
|----------|-------------|---------|
| `VAULT_WATCH_DIR` | Container path to watch for documents (maps to `/watch` in container) | `/watch` |
| `VAULT_PATH` | Container path for vault storage (maps to `/vault` in container) | `/vault` |
| `VAULT_HOST` | Web server bind address | `0.0.0.0` |
| `VAULT_PORT` | Web server port | `8002` |
| `OPENAI_API_KEY` | OpenAI API key (optional, required if using OpenAI models) | - |
| `OLLAMA_BASE_URL` | Ollama server URL (optional, required if using Ollama models) | `http://localhost:11434` |
| `TALKPIPE_EMBEDDING_MODEL` | Embedding model name (e.g., `text-embedding-3-large`, `embeddinggemma`) | `embeddinggemma` |
| `TALKPIPE_EMBEDDING_SOURCE` | Embedding provider (`openai`, `ollama`, etc.) | `ollama` |
| `TALKPIPE_CHAT_MODEL` | Chat/completion model name (e.g., `gpt-4`, `mistral-small`) | `mistral-small` |
| `TALKPIPE_CHAT_SOURCE` | Chat provider (`openai`, `ollama`, etc.) | `ollama` |
| `TALKPIPE_DOCUMENT_TEMPLATE` | Template for formatting documents (optional) | `"title: {title} \| text: {content}"` |
| `TALKPIPE_SHINGLE_TEMPLATE` | Template for formatting shingled chunks (optional) | `"title: {title} \| text: {shingle}"` |
| `TALKPIPE_RETRIEVAL_TEMPLATE` | Template for formatting search queries (optional) | `"task: search result \| query: {query}"` |

### Example .env File

```env
# Container paths (these are fixed - /watch and /vault inside the container)
# The host paths are specified in the volume mounts when running the container
VAULT_WATCH_DIR=/watch
VAULT_PATH=/vault

# Web server
VAULT_HOST=0.0.0.0
VAULT_PORT=8002

# AI providers (pick one or both)
OPENAI_API_KEY=sk-your-openai-key
OLLAMA_BASE_URL=http://host.containers.internal:11434

# Optional: Model configuration (if not set, uses defaults)
# TALKPIPE_EMBEDDING_MODEL=text-embedding-3-large
# TALKPIPE_EMBEDDING_SOURCE=openai
# TALKPIPE_CHAT_MODEL=gpt-4
# TALKPIPE_CHAT_SOURCE=openai
```

### Volume Mounts

The container requires two volume mounts:

1. **Watch directory** (`/watch` in container):
   - Place documents here to be indexed
   - Watcher monitors this directory for changes
   - Can use absolute or relative paths

2. **Vault directory** (`/vault` in container):
   - Stores LanceDB vector database (`/vault/vector_vault/`)
   - Stores Whoosh full-text index (`/vault/fulltext_vault/`)
   - Must be persistent for data retention

### Container Behavior

When the container starts:
1. File watcher begins monitoring `VAULT_WATCH_DIR`
2. Web application starts on `VAULT_HOST:VAULT_PORT`
3. Documents in watch directory are automatically indexed to `VAULT_PATH`
4. Web UI becomes available for searching and querying

## Data Persistence

### What Gets Stored

Vault data is stored in the vault directory you mount (e.g., `./vault` or `/path/to/vault`):

```
vault/
├── vector_vault/
│   ├── full_documents/    # Full document embeddings
│   └── shingled_chunks/   # Chunk embeddings for precise retrieval
└── fulltext_vault/        # Whoosh full-text search index
```

### Backup and Restore

Since everything is in directories, backups are simple file copies:

```bash
# Backup
cd ~/my-vault-deployment
tar -czf vault-backup-$(date +%Y%m%d).tar.gz vault/

# Or just copy
cp -r vault vault-backup-$(date +%Y%m%d)

# Restore
tar -xzf vault-backup-20251220.tar.gz
# Or
cp -r vault-backup-20251220 vault
```

### Moving to Another System

1. Stop the container
2. Copy the `.env` file and `vault/` directory to the new system
3. Start the container with the same configuration

```bash
# On old system
docker stop talkpipe-vault
tar -czf deployment.tar.gz .env vault/

# On new system
tar -xzf deployment.tar.gz
docker run -d --name talkpipe-vault --env-file .env \
  -p 8002:8002 \
  -v "$(pwd)/vault:/vault" \
  -v "$(pwd)/watch:/watch" \
  talkpipe-vault:latest
```

## Common Usage Patterns

### Pattern 1: Single Deployment Directory

```bash
mkdir ~/talkpipe-production
cd ~/talkpipe-production

# Create .env with container paths
cat > .env << 'EOF'
VAULT_WATCH_DIR=/watch
VAULT_PATH=/vault
VAULT_PORT=8002
OPENAI_API_KEY=sk-...
EOF

# Run (host paths specified in volume mounts)
podman run -d --name vault --userns=keep-id --env-file .env \
  -p 8002:8002 \
  -v /home/user/documents:/watch:Z \
  -v /home/user/vault-data:/vault:Z \
  talkpipe-vault:latest
```

### Pattern 2: Multiple Vaults (Different Ports)

```bash
# Vault 1: Personal documents on port 8002
mkdir -p ~/vault1 && cd ~/vault1
cat > .env << EOF
VAULT_WATCH_DIR=/watch
VAULT_PATH=/vault
VAULT_PORT=8002
EOF

podman run -d --name vault-personal --userns=keep-id --env-file .env \
  -p 8002:8002 \
  -v "$(pwd)/watch:/watch:Z" \
  -v "$(pwd)/vault:/vault:Z" \
  talkpipe-vault:latest

# Vault 2: Work documents on port 8003
mkdir -p ~/vault2 && cd ~/vault2
cat > .env << EOF
VAULT_WATCH_DIR=/watch
VAULT_PATH=/vault
VAULT_PORT=8003
EOF

podman run -d --name vault-work --userns=keep-id --env-file .env \
  -p 8003:8003 \
  -v "$(pwd)/watch:/watch:Z" \
  -v "$(pwd)/vault:/vault:Z" \
  talkpipe-vault:latest
```

### Pattern 3: Read-Only Watch Directory

If you want to index existing documents without moving them:

```bash
podman run -d --name vault --userns=keep-id --env-file .env \
  -p 8002:8002 \
  -v /path/to/readonly/docs:/watch:ro,Z \
  -v "$(pwd)/vault:/vault:Z" \
  talkpipe-vault:latest
```

The `:ro` flag makes the watch directory read-only in the container.

## Security Best Practices

### 1. Network Security

If exposing publicly, use HTTPS via a reverse proxy:

```yaml
# Example nginx reverse proxy config
server {
    listen 443 ssl;
    server_name vault.example.com;

    ssl_certificate /etc/ssl/certs/cert.pem;
    ssl_certificate_key /etc/ssl/private/key.pem;

    location / {
        proxy_pass http://localhost:8002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Restrict access to localhost only:

```bash
# Only accessible from host machine
docker run -d -p 127.0.0.1:8002:8002 ...
```

### 2. Filesystem Security

Protect your vault and watch directories with appropriate permissions:

```bash
# Restrict directory access
chmod 700 vault/ watch/

# Or use specific user ownership
sudo chown -R myuser:myuser vault/ watch/
chmod 750 vault/ watch/
```

For Podman with SELinux, the `:Z` flag handles context automatically. For Docker on SELinux systems:

```bash
# Set SELinux context if needed
chcon -R -t container_file_t vault/ watch/
```

### 3. Environment Variable Security

Never commit `.env` files with secrets to version control:

```bash
# Add to .gitignore
echo ".env" >> .gitignore
echo ".env.local" >> .gitignore
```

Use environment-specific files:

```bash
.env.example     # Template (commit this)
.env.local       # Local dev (ignore)
.env.production  # Production (ignore, deploy securely)
```

### 4. Container Security

Run with read-only root filesystem where possible:

```bash
podman run -d --read-only \
  --tmpfs /tmp:rw,noexec,nosuid \
  --tmpfs /tmp/numba_cache:rw,noexec,nosuid \
  ...
```

Use non-root user (already configured in Dockerfile) and limit resources:

```bash
docker run -d \
  --memory=2g \
  --cpus=1.5 \
  ...
```

### 5. Single-User Deployment

This application has **no built-in authentication**. It's designed for single-user or trusted network use. For multi-user scenarios:

- Deploy behind an authenticating reverse proxy (OAuth2, LDAP, etc.)
- Use VPN or network isolation
- Consider adding application-level auth if needed

## Troubleshooting

### Container Won't Start

Check logs for errors:

```bash
docker logs talkpipe-vault
# Or
podman logs talkpipe-vault
```

Common issues:

**Port already in use:**
```bash
# Find what's using the port
sudo lsof -i :8002
# Or change port in .env
VAULT_PORT=8003
```

**Volume permission errors (Podman):**
```bash
# Option 1: Use --userns=keep-id (RECOMMENDED for Podman)
# This maps your host user's UID/GID into the container
podman run -d --name talkpipe-vault --userns=keep-id \
  --env-file .env \
  -p 8002:8002 \
  -v "$(pwd)/watch:/watch:Z" \
  -v "$(pwd)/vault:/vault:Z" \
  talkpipe-vault:latest

# Option 2: Fix permissions on host directories
# Make sure the vault directory is writable by your user
chmod -R u+rwX "$(pwd)/vault"
chmod -R u+rX "$(pwd)/watch"

# Option 3: Run as root (NOT RECOMMENDED for security)
podman run --user 0:0 -v "$(pwd)/vault:/vault:Z" ...

# Option 4: Export UID/GID and use docker-compose
export UID=$(id -u)
export GID=$(id -g)
podman-compose up --userns=keep-id
```

**Watch directory not accessible:**
```bash
# Check permissions
ls -la watch/
# Fix ownership if needed
sudo chown -R $(id -u):$(id -g) watch/
```

### File Watcher Not Detecting Changes

The watcher uses polling mode by default (works on all filesystems including NFS). If files aren't being detected:

1. Check the watch directory is mounted correctly:
   ```bash
   docker exec talkpipe-vault ls -la /watch
   ```

2. Verify watcher is running:
   ```bash
   docker logs talkpipe-vault | grep -i "watcher"
   ```

3. Test with a simple file:
   ```bash
   echo "test" > watch/test.txt
   # Check logs for processing
   docker logs -f talkpipe-vault
   ```

### Web Application Not Accessible

**Check the container is running:**
```bash
docker ps | grep talkpipe-vault
```

**Verify port mapping:**
```bash
docker port talkpipe-vault
```

**Test from inside container:**
```bash
docker exec talkpipe-vault curl -f http://localhost:8002 || echo "Not responding"
```

**Check firewall:**
```bash
sudo firewall-cmd --list-ports
# If needed:
sudo firewall-cmd --add-port=8002/tcp --permanent
sudo firewall-cmd --reload
```

### Index Lock Issues

If you see `PermissionError: [Errno 13] Permission denied: '/vault/fulltext_vault/MAIN_WRITELOCK'`:

This is usually a permissions issue where the container user cannot write to the mounted vault directory.

**Why this happens:**

The error `PermissionError: [Errno 13] Permission denied: '/vault/fulltext_vault/MAIN_WRITELOCK'` occurs when Whoosh tries to **create** the lock file, but the container user cannot write to the directory. This happens even if the lock file doesn't exist yet - it's a directory permission issue, not a file permission issue.

**Step-by-step fix:**

**1. Stop the container:**
```bash
# If using podman/docker directly
podman stop talkpipe-vault
podman rm talkpipe-vault

# If using docker-compose
docker-compose down
# or
podman compose down
```

**2. Verify your user ID matches:**
```bash
# Check your user ID
id -u
# Should match the container user when using --userns=keep-id

# Check directory ownership
ls -ld /home/travis/vaultdb/fulltext_vault
# Owner should match your user
```

**3. Fix host directory permissions (CRITICAL):**
```bash
# Ensure the vault directory is owned by your user and has write permissions
# Replace /home/travis/vaultdb with your actual vault path
sudo chown -R $(id -u):$(id -g) /home/travis/vaultdb

# Ensure the directory itself is writable (not just readable)
chmod -R u+rwX /home/travis/vaultdb

# Verify the fulltext_vault directory is writable:
touch /home/travis/vaultdb/fulltext_vault/.test_write 2>/dev/null && rm /home/travis/vaultdb/fulltext_vault/.test_write && echo "Directory is writable" || echo "Directory is NOT writable - fix permissions"

# Also fix watch directory if needed
sudo chown -R $(id -u):$(id -g) /mnt/gonk-shared/research_papers
chmod -R u+rX /mnt/gonk-shared/research_papers
```

**4. Clean up any stale lock files:**
```bash
# Remove all lock files from the vault directory
find /home/travis/vaultdb/fulltext_vault -name "*LOCK" -type f -delete 2>/dev/null || true

# Or if you need to force removal:
sudo find /home/travis/vaultdb/fulltext_vault -name "*LOCK" -type f -delete
```

**4. For Podman with docker-compose:**
The `userns: keep-id` setting in docker-compose.yml should handle this, but make sure:
- Export your UID/GID before running compose:
  ```bash
  export UID=$(id -u)
  export GID=$(id -g)
  podman compose up -d
  ```

**5. For Podman with direct run commands:**
```bash
# Always use --userns=keep-id
podman run -d --name talkpipe-vault --userns=keep-id \
  --env-file .env \
  -p 8002:8002 \
  -v "$(pwd)/watch:/watch:Z" \
  -v "$(pwd)/vault:/vault:Z" \
  talkpipe-vault:latest
```

**6. For Docker (without Podman):**
```bash
# Option A: Change ownership to UID 1001 (container user)
sudo chown -R 1001:1001 /home/travis/vaultdb

# Option B: Make directories world-writable (less secure, not recommended)
sudo chmod -R 777 /home/travis/vaultdb
```

**7. Rebuild the image (if entrypoint.sh was updated):**
```bash
# In the talkpipe-vault repository directory
docker build -t talkpipe-vault:latest .
# or
podman build -t talkpipe-vault:latest .
```

**8. Restart the container:**
```bash
# With docker-compose
docker-compose up -d
# or
podman compose up -d

# Or with direct run command (see step 5)
```

The entrypoint script automatically attempts to clean up stale lock files on startup, but it cannot override host-level permission restrictions. Always fix permissions on the host first.

### High Memory/CPU Usage

Limit container resources:

```bash
docker run -d \
  --memory=2g \
  --cpus=2 \
  --env-file .env \
  ...
```

Or in docker-compose.yml:

```yaml
services:
  vault:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
```

### Viewing Container Internals

```bash
# Interactive shell
docker exec -it talkpipe-vault /bin/bash

# Check vault contents
docker exec talkpipe-vault ls -lR /vault

# Check processes
docker exec talkpipe-vault ps aux
```

## Upgrading

### Upgrading the Container

To upgrade to a new version:

1. **Rebuild the image:**
   ```bash
   cd /path/to/talkpipe-vault-repo
   git pull origin main
   docker build -t talkpipe-vault:latest .
   ```

2. **Stop and remove old container:**
   ```bash
   docker stop talkpipe-vault
   docker rm talkpipe-vault
   ```

3. **Start new container** (your vault data is preserved in the volume):
   ```bash
   cd ~/my-vault-deployment
   docker run -d --name talkpipe-vault --env-file .env \
     -p 8002:8002 \
     -v "$(pwd)/watch:/watch" \
     -v "$(pwd)/vault:/vault" \
     talkpipe-vault:latest
   ```

**Note:** Vault data (indices) are preserved across upgrades. However, major version changes might require re-indexing. Check release notes.

### Testing Before Production

Build and test experimental versions:

```bash
# Build experimental
docker build -t talkpipe-vault:experimental .

# Test in separate directory
mkdir ~/vault-test && cd ~/vault-test
mkdir watch vault
# Copy .env and modify for testing
cp ~/production-vault/.env .env

# Run experimental
docker run -d --name vault-test --env-file .env \
  -p 8003:8002 \
  -v "$(pwd)/watch:/watch" \
  -v "$(pwd)/vault:/vault" \
  talkpipe-vault:experimental

# Test, then clean up
docker stop vault-test
docker rm vault-test
```

## Advanced Configuration

### Custom AI Models

TalkPipe Vault supports configuring models via TalkPipe configuration. You can set model names and sources using environment variables with the `TALKPIPE_` prefix, or by mounting a TalkPipe config file.

#### Configuration Methods

**Method 1: Environment Variables (Recommended for Docker)**

Add to your `.env` file:

```env
# Model configuration via TalkPipe environment variables
TALKPIPE_EMBEDDING_MODEL=text-embedding-3-large
TALKPIPE_EMBEDDING_SOURCE=openai
TALKPIPE_CHAT_MODEL=gpt-4
TALKPIPE_CHAT_SOURCE=openai

# Or for Ollama
TALKPIPE_EMBEDDING_MODEL=embeddinggemma
TALKPIPE_EMBEDDING_SOURCE=ollama
TALKPIPE_CHAT_MODEL=mistral-small
TALKPIPE_CHAT_SOURCE=ollama

# Provider-specific configuration
OPENAI_API_KEY=sk-your-actual-key-here
OLLAMA_BASE_URL=http://host.containers.internal:11434
```

**Method 2: TalkPipe Config File**

Mount a TalkPipe config file into the container:

```bash
docker run -d \
  --name talkpipe-vault \
  --env-file .env \
  -v "$(pwd)/watch:/watch" \
  -v "$(pwd)/vault:/vault" \
  -v "$HOME/.talkpipe.toml:/home/app/.talkpipe.toml:ro" \
  talkpipe-vault:latest
```

With `~/.talkpipe.toml` containing:

```toml
[vault]
embedding_model = "text-embedding-3-large"
embedding_source = "openai"
chat_model = "gpt-4"
chat_source = "openai"
```

#### Using Local Ollama

In your `.env`:
```env
# Point to Ollama on host machine
OLLAMA_BASE_URL=http://host.containers.internal:11434

# Configure models to use Ollama
TALKPIPE_EMBEDDING_MODEL=embeddinggemma
TALKPIPE_EMBEDDING_SOURCE=ollama
TALKPIPE_CHAT_MODEL=mistral-small
TALKPIPE_CHAT_SOURCE=ollama
```

Make sure Ollama is running on your host:
```bash
# On host machine
ollama serve
```

#### Using OpenAI

```env
# OpenAI API key
OPENAI_API_KEY=sk-your-actual-key-here

# Configure models to use OpenAI
TALKPIPE_EMBEDDING_MODEL=text-embedding-3-large
TALKPIPE_EMBEDDING_SOURCE=openai
TALKPIPE_CHAT_MODEL=gpt-4
TALKPIPE_CHAT_SOURCE=openai
```

#### Custom Templates

You can also customize the templates used for formatting text before embedding:

```env
# Custom templates
TALKPIPE_DOCUMENT_TEMPLATE="Document: {title}\nContent: {content}"
TALKPIPE_SHINGLE_TEMPLATE="Chunk from {title}: {shingle}"
TALKPIPE_RETRIEVAL_TEMPLATE="Search for: {query}"
```

See the [README.md](README.md) for details on template placeholders and configuration options.

### Running Additional Commands

Execute one-off indexing jobs in the running container:

```bash
# Index a specific directory once
docker exec talkpipe-vault vault-list-into-vectordb \
  "/watch/**/*.pdf" \
  --vault-path /vault \
  --overwrite

# Check vault statistics
docker exec talkpipe-vault ls -lh /vault/vector_vault/
```

### Accessing Logs and Metrics

```bash
# Real-time logs
docker logs -f talkpipe-vault

# Last 100 lines
docker logs --tail 100 talkpipe-vault

# Since timestamp
docker logs --since 2025-12-20T10:00:00 talkpipe-vault

# Resource usage
docker stats talkpipe-vault
```

## Production Deployment Example

Complete example for a production deployment:

### Directory Structure

```
~/production-vault/
├── .env                    # Configuration
├── watch/                  # Input: documents to index
├── vault/                  # Output: indices (persistent)
│   ├── vector_vault/
│   └── fulltext_vault/
└── backups/               # Optional: backup storage
```

### Production .env

```env
# production-vault/.env
# Container paths (these are fixed - /watch and /vault inside the container)
VAULT_WATCH_DIR=/watch
VAULT_PATH=/vault
VAULT_HOST=0.0.0.0
VAULT_PORT=8002

# AI configuration
OPENAI_API_KEY=sk-prod-key-here

# Model configuration (optional, uses defaults if not set)
TALKPIPE_EMBEDDING_MODEL=text-embedding-3-large
TALKPIPE_EMBEDDING_SOURCE=openai
TALKPIPE_CHAT_MODEL=gpt-4
TALKPIPE_CHAT_SOURCE=openai

# Optional: resource limits set via docker run flags
```

### Systemd Service (Optional)

For automatic startup on system boot:

```ini
# /etc/systemd/system/talkpipe-vault.service
[Unit]
Description=TalkPipe Vault Document Search
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/user/production-vault
ExecStart=/usr/bin/docker run -d \
  --name talkpipe-vault \
  --env-file /home/user/production-vault/.env \
  --restart unless-stopped \
  -p 8002:8002 \
  -v /home/user/production-vault/watch:/watch \
  -v /home/user/production-vault/vault:/vault \
  talkpipe-vault:latest

ExecStop=/usr/bin/docker stop talkpipe-vault
ExecStopPost=/usr/bin/docker rm -f talkpipe-vault

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable talkpipe-vault.service
sudo systemctl start talkpipe-vault.service
sudo systemctl status talkpipe-vault.service
```

### Automated Backups

```bash
# /home/user/backup-vault.sh
#!/bin/bash
DEPLOY_DIR="/home/user/production-vault"
BACKUP_DIR="/home/user/backups"
DATE=$(date +%Y%m%d-%H%M%S)

# Create backup
tar -czf "${BACKUP_DIR}/vault-${DATE}.tar.gz" \
  -C "${DEPLOY_DIR}" vault/

# Keep only last 7 days
find "${BACKUP_DIR}" -name "vault-*.tar.gz" -mtime +7 -delete

echo "Backup completed: vault-${DATE}.tar.gz"
```

Add to crontab:
```bash
# Run daily at 2 AM
0 2 * * * /home/user/backup-vault.sh >> /var/log/vault-backup.log 2>&1
```

### Nginx Reverse Proxy with SSL

```nginx
# /etc/nginx/sites-available/vault
server {
    listen 80;
    server_name vault.example.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name vault.example.com;

    ssl_certificate /etc/letsencrypt/live/vault.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/vault.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # Optional: Basic auth
    auth_basic "Vault Access";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://localhost:8002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable:
```bash
sudo ln -s /etc/nginx/sites-available/vault /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## Reference

### Container Architecture

- **Base Image**: Fedora (latest)
- **Runtime User**: `app` (UID 1001, non-root)
- **Exposed Port**: 8002 (configurable via `VAULT_PORT`)
- **Volume Mounts**:
  - `/watch`: Watch directory for incoming documents
  - `/vault`: Persistent storage for indices

### Startup Sequence

1. Container starts with `/app/entrypoint.sh`
2. File watcher (`vault-watch-into-vectordb`) starts in background
3. Web application (`vault-query`) starts in foreground
4. Health check validates container is running

### Health Check

The container includes a health check that runs every 30 seconds:
```bash
python3 -c "import talkpipe_vault; print('OK')"
```

Check health status:
```bash
docker inspect --format='{{.State.Health.Status}}' talkpipe-vault
```

### Environment Variable Reference

| Variable | Container Path | Purpose |
|----------|---------------|---------|
| `VAULT_WATCH_DIR` | Maps to `/watch` | Input documents |
| `VAULT_PATH` | Maps to `/vault` | Index storage |
| `VAULT_HOST` | Used by web app | Bind address |
| `VAULT_PORT` | Used by web app | Listen port |
| `OPENAI_API_KEY` | Used by AI pipelines | OpenAI authentication |
| `OLLAMA_BASE_URL` | Used by AI pipelines | Local LLM endpoint |
| `TALKPIPE_EMBEDDING_MODEL` | TalkPipe config | Embedding model name |
| `TALKPIPE_EMBEDDING_SOURCE` | TalkPipe config | Embedding provider |
| `TALKPIPE_CHAT_MODEL` | TalkPipe config | Chat model name |
| `TALKPIPE_CHAT_SOURCE` | TalkPipe config | Chat provider |
| `TALKPIPE_*_TEMPLATE` | TalkPipe config | Text formatting templates |

### Entrypoint Script

The entrypoint (`/app/entrypoint.sh`) can be overridden:

```bash
# Run only the web app (no watcher)
docker run -d talkpipe-vault:latest \
  vault-query /vault --host 0.0.0.0 --port 8002

# Run only the watcher
docker run -d talkpipe-vault:latest \
  vault-watch-into-vectordb /watch --vault-path /vault --polling

# Custom command
docker run -it talkpipe-vault:latest /bin/bash
```

## Additional Resources

- **Source Code**: [github.com/sandialabs/talkpipe-vault](https://github.com/sandialabs/talkpipe-vault)
- **TalkPipe Framework**: [github.com/sandialabs/talkpipe](https://github.com/sandialabs/talkpipe)
- **API Documentation**: Available at `http://localhost:8002/docs` when running
- **README**: See [README.md](README.md) for application features and usage
