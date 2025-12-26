#!/bin/bash
set -e

# Default values
VAULT_PATH="${VAULT_PATH:-/vault}"
VAULT_WATCH_DIR="${VAULT_WATCH_DIR:-/watch}"
VAULT_HOST="${VAULT_HOST:-0.0.0.0}"
VAULT_PORT="${VAULT_PORT:-8002}"

echo "Starting TalkPipe Vault..."
echo "  Vault storage: ${VAULT_PATH}"
echo "  Watch directory: ${VAULT_WATCH_DIR}"
echo "  Web server: ${VAULT_HOST}:${VAULT_PORT}"

# Ensure vault directory exists and has correct permissions
echo "Setting up vault directory..."
mkdir -p "${VAULT_PATH}/vector_vault"
mkdir -p "${VAULT_PATH}/fulltext_vault"

# Fix permissions on vault directory (in case of permission issues)
# This is safe even if running as root
chmod -R u+rwX "${VAULT_PATH}" 2>/dev/null || true

# Clean up any stale Whoosh lock files that might cause permission errors
# Delete all lock files on startup since we're starting fresh
echo "Checking for stale lock files..."
if [ -d "${VAULT_PATH}/fulltext_vault" ]; then
    # Remove all lock files (they'll be recreated if needed)
    find "${VAULT_PATH}/fulltext_vault" -name "*WRITELOCK" -type f -delete 2>/dev/null || true
    find "${VAULT_PATH}/fulltext_vault" -name "*READLOCK" -type f -delete 2>/dev/null || true
    echo "Cleaned up stale lock files"
fi

# Check watch directory permissions
echo "Checking watch directory..."
if [ ! -r "${VAULT_WATCH_DIR}" ]; then
    echo "WARNING: Watch directory ${VAULT_WATCH_DIR} is not readable"
    echo "Attempting to fix permissions..."
    chmod -R u+rX "${VAULT_WATCH_DIR}" 2>/dev/null || true
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
