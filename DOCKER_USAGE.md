# Docker Usage Guide

This guide explains how to build and use the Docker images for testing and running TalkPipe Vault.

## Building Images

### Build Runtime Image

Build the production runtime image:

```bash
# From the project root directory
docker build -t talkpipe-vault:latest --target runtime .
# Or with Podman
podman build -t talkpipe-vault:latest --target runtime .
```

## Running Unit Tests

The builder stage automatically runs unit tests during the build process. To run tests, you can use the builder stage:

```bash
# Build the builder stage (which runs tests automatically)
docker build -t talkpipe-vault:builder --target builder .

# Or to just run tests interactively in the builder image
docker build -t talkpipe-vault:builder --target builder .
docker run --rm talkpipe-vault:builder python3 -m pytest tests/ -v
```

Alternatively, you can run tests directly without Docker:

```bash
# Install dependencies and run tests locally
pip install -e .[dev,docling]
pytest tests/
```

## Running the Application from Another Directory

You can create a deployment directory anywhere and use an `.env` file to configure the application.

### Step 1: Create Deployment Directory

```bash
# Create your deployment directory
mkdir -p ~/my-vault-deployment
cd ~/my-vault-deployment

# Create subdirectories for watch and vault data
mkdir -p watch vault
```

### Step 2: Create .env File

Create a `.env` file in your deployment directory:

```bash
cat > .env << 'EOF'
# Container paths (these are fixed - /watch and /vault inside the container)
VAULT_WATCH_DIR=/watch
VAULT_PATH=/vault

# Web server configuration
VAULT_HOST=0.0.0.0
VAULT_PORT=8002

# Optional: AI provider configuration
OPENAI_API_KEY=sk-your-key-here
# OLLAMA_BASE_URL=http://host.containers.internal:11434
EOF
```

**Important**: The `VAULT_WATCH_DIR` and `VAULT_PATH` in the `.env` file should be the container paths (`/watch` and `/vault`), not the host paths. The host paths are specified in the volume mounts when running the container.

### Step 3: Run the Container

#### Using Docker

```bash
docker run -d \
  --name talkpipe-vault \
  --env-file .env \
  -p 8002:8002 \
  -v "$(pwd)/watch:/watch" \
  -v "$(pwd)/vault:/vault" \
  talkpipe-vault:latest
```

#### Using Podman

```bash
podman run -d \
  --name talkpipe-vault \
  --env-file .env \
  -p 8002:8002 \
  -v "$(pwd)/watch:/watch:Z" \
  -v "$(pwd)/vault:/vault:Z" \
  talkpipe-vault:latest
```

**Note**: The `:Z` suffix on Podman volumes sets the correct SELinux context for container access.

### Step 4: Using Absolute Host Paths

If you want to use absolute paths on the host filesystem, mount them directly:

```bash
# Example: Use existing directories on your system
docker run -d \
  --name talkpipe-vault \
  --env-file .env \
  -p 8002:8002 \
  -v "/home/user/documents:/watch" \
  -v "/home/user/vault-data:/vault" \
  talkpipe-vault:latest
```

Or update your `.env` file to use absolute host paths and mount them:

```bash
# .env file with comments
# Container paths (always /watch and /vault)
VAULT_WATCH_DIR=/watch
VAULT_PATH=/vault
VAULT_PORT=8002
```

Then mount your actual host directories:

```bash
docker run -d \
  --name talkpipe-vault \
  --env-file .env \
  -p 8002:8002 \
  -v "/path/to/your/documents:/watch" \
  -v "/path/to/your/vault:/vault" \
  talkpipe-vault:latest
```

## Viewing Logs

```bash
# Docker
docker logs -f talkpipe-vault

# Podman
podman logs -f talkpipe-vault
```

## Stopping the Container

```bash
# Docker
docker stop talkpipe-vault
docker rm talkpipe-vault

# Podman
podman stop talkpipe-vault
podman rm talkpipe-vault
```

## Accessing the Web Interface

Once the container is running, access the web interface at:

```
http://localhost:8002
```

## Troubleshooting

### Docling Not Working

The runtime image now includes docling with all its dependencies. If you encounter issues:

1. Verify docling is installed: `docker exec talkpipe-vault python3 -c "import docling; print('OK')"`
2. Check logs for import errors: `docker logs talkpipe-vault`

### Permission Issues (Podman)

If you encounter permission issues with Podman, ensure you're using the `:Z` flag on volume mounts, or run with `--privileged` flag (less secure):

```bash
podman run --privileged ...
```

### Port Already in Use

If port 8002 is already in use, change it in your `.env` file:

```bash
VAULT_PORT=8003
```

And update the port mapping:

```bash
docker run ... -p 8003:8003 ...
```

