#!/bin/bash
set -e

# Default values
VAULT_PATH="${VAULT_PATH:-/app/data/vault}"
VAULT_WATCH_DIR="${VAULT_WATCH_DIR:-/app/data/watch}"
VAULT_HOST="${VAULT_HOST:-0.0.0.0}"
VAULT_PORT="${VAULT_PORT:-8002}"

echo "Starting TalkPipe Vault..."
echo "  Vault storage: ${VAULT_PATH}"
echo "  Watch directory: ${VAULT_WATCH_DIR}"
echo "  Web server: ${VAULT_HOST}:${VAULT_PORT}"

# Ensure directories exist (in case volume mount is empty)
mkdir -p "${VAULT_PATH}" 2>/dev/null || true
mkdir -p "${VAULT_WATCH_DIR}" 2>/dev/null || true

# Check permissions
if [ ! -w "${VAULT_PATH}" ]; then
    echo "ERROR: Vault directory ${VAULT_PATH} is not writable."
    echo "       Current user: $(id -u):$(id -g)"
    echo "       Directory permissions: $(stat -c '%a %U:%G' "${VAULT_PATH}" 2>/dev/null || echo 'unknown')"
    echo "       Please ensure the volume is mounted with correct permissions."
    echo "       For Podman, use --userns=keep-id."
    exit 1
fi

# Start the file watcher in the background
echo "Starting file watcher..."
vault-watch-into-vectordb "${VAULT_WATCH_DIR}" \
    --vault-path "${VAULT_PATH}" \
    --polling \
    --debounce-seconds 2.0 \
    &

WATCHER_PID=$!
echo "File watcher started (PID: ${WATCHER_PID})"

# Give the watcher a moment to initialize
sleep 2

# Start the web application in the foreground
echo "Starting web application..."
exec vault-query "${VAULT_PATH}" \
    --host "${VAULT_HOST}" \
    --port "${VAULT_PORT}"
