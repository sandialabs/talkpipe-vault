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
if ! mkdir -p "${VAULT_PATH}/vector_vault" 2>/dev/null; then
    echo "ERROR: Cannot create ${VAULT_PATH}/vector_vault"
    echo "       Check that the vault directory is writable by the container user"
    echo "       For Podman, use --userns=keep-id flag"
    exit 1
fi

if ! mkdir -p "${VAULT_PATH}/fulltext_vault" 2>/dev/null; then
    echo "ERROR: Cannot create ${VAULT_PATH}/fulltext_vault"
    echo "       Check that the vault directory is writable by the container user"
    echo "       For Podman, use --userns=keep-id flag"
    exit 1
fi

# Fix permissions on vault directory (in case of permission issues)
# This will fail silently if we don't have permission, which is expected
chmod -R u+rwX "${VAULT_PATH}" 2>/dev/null || true

# Verify we can actually write to the vault directory and subdirectories
if [ ! -w "${VAULT_PATH}" ]; then
    echo "ERROR: Vault directory ${VAULT_PATH} is not writable"
    echo "       Current user: $(id -u):$(id -g)"
    echo "       Directory owner: $(stat -c '%U:%G (%u:%g)' "${VAULT_PATH}" 2>/dev/null || echo 'unknown')"
    echo ""
    echo "Solutions:"
    echo "  For Podman: Add --userns=keep-id to your podman run command"
    echo "  For Docker: Ensure the vault directory is owned by UID 1001 or is world-writable"
    echo "  Or fix permissions on host: chmod -R u+rwX $(realpath ${VAULT_PATH})"
    exit 1
fi

# CRITICAL: Test that we can actually CREATE files in the fulltext_vault directory
# This is what Whoosh needs to create lock files
TEST_LOCK_FILE="${VAULT_PATH}/fulltext_vault/.write_test_$(date +%s)"
if ! touch "$TEST_LOCK_FILE" 2>/dev/null; then
    echo "ERROR: Cannot create files in ${VAULT_PATH}/fulltext_vault"
    echo "       Current user: $(id -u):$(id -g)"
    echo "       Directory owner: $(stat -c '%U:%G (%u:%g)' "${VAULT_PATH}/fulltext_vault" 2>/dev/null || echo 'unknown')"
    echo "       Directory permissions: $(stat -c '%a' "${VAULT_PATH}/fulltext_vault" 2>/dev/null || echo 'unknown')"
    echo ""
    echo "This is why Whoosh cannot create lock files. The directory exists but is not writable."
    echo ""
    echo "Solutions:"
    echo "  For Podman with docker-compose:"
    echo "    export UID=$(id -u)"
    echo "    export GID=$(id -g)"
    echo "    podman compose down"
    echo "    podman compose up -d"
    echo ""
    echo "  For Podman direct run: Use --userns=keep-id flag"
    echo ""
    echo "  Fix permissions on host:"
    echo "    sudo chown -R \$(id -u):\$(id -g) $(realpath ${VAULT_PATH})"
    echo "    chmod -R u+rwX $(realpath ${VAULT_PATH})"
    exit 1
fi
# Clean up test file
rm -f "$TEST_LOCK_FILE" 2>/dev/null || true

# Also verify vector_vault is writable
TEST_VECTOR_FILE="${VAULT_PATH}/vector_vault/.write_test_$(date +%s)"
if ! touch "$TEST_VECTOR_FILE" 2>/dev/null; then
    echo "ERROR: Cannot create files in ${VAULT_PATH}/vector_vault"
    echo "       Current user: $(id -u):$(id -g)"
    echo "       Directory owner: $(stat -c '%U:%G (%u:%g)' "${VAULT_PATH}/vector_vault" 2>/dev/null || echo 'unknown')"
    echo "       Fix permissions on host: chmod -R u+rwX $(realpath ${VAULT_PATH}/vector_vault)"
    rm -f "$TEST_VECTOR_FILE" 2>/dev/null || true
    exit 1
fi
rm -f "$TEST_VECTOR_FILE" 2>/dev/null || true

# Clean up any stale Whoosh lock files that might cause permission errors
# Delete all lock files on startup since we're starting fresh
echo "Checking for stale lock files..."
if [ -d "${VAULT_PATH}/fulltext_vault" ]; then
    # Try to remove lock files - first attempt with standard delete
    find "${VAULT_PATH}/fulltext_vault" -name "*WRITELOCK" -type f -delete 2>/dev/null || true
    find "${VAULT_PATH}/fulltext_vault" -name "*READLOCK" -type f -delete 2>/dev/null || true
    
    # If lock files still exist and we can't delete them, try to fix permissions first
    LOCK_FILES=$(find "${VAULT_PATH}/fulltext_vault" -name "*LOCK" -type f 2>/dev/null || true)
    if [ -n "$LOCK_FILES" ]; then
        echo "Found lock files that couldn't be deleted automatically, attempting to fix permissions..."
        # Try to change ownership to current user (works if we have permission)
        echo "$LOCK_FILES" | while read -r lockfile; do
            if [ -f "$lockfile" ]; then
                # Try to make it writable and delete
                chmod 666 "$lockfile" 2>/dev/null || true
                rm -f "$lockfile" 2>/dev/null || true
            fi
        done
        # Final check - warn if lock files still exist
        REMAINING_LOCKS=$(find "${VAULT_PATH}/fulltext_vault" -name "*LOCK" -type f 2>/dev/null || true)
        if [ -n "$REMAINING_LOCKS" ]; then
            echo "WARNING: Could not remove all lock files. They may cause permission errors."
            echo "         Remaining lock files:"
            echo "$REMAINING_LOCKS" | while read -r lockfile; do
                echo "           $lockfile (owner: $(stat -c '%U:%G' "$lockfile" 2>/dev/null || echo 'unknown'))"
            done
            echo "         You may need to manually remove them or fix permissions on the host."
        else
            echo "Cleaned up stale lock files"
        fi
    else
        echo "Cleaned up stale lock files"
    fi
fi

# Check watch directory permissions
echo "Checking watch directory..."
if [ ! -r "${VAULT_WATCH_DIR}" ]; then
    echo "WARNING: Watch directory ${VAULT_WATCH_DIR} is not readable"
    echo "         Current user: $(id -u):$(id -g)"
    echo "         Directory owner: $(stat -c '%U:%G (%u:%g)' "${VAULT_WATCH_DIR}" 2>/dev/null || echo 'unknown')"
    echo "Attempting to fix permissions..."
    if ! chmod -R u+rX "${VAULT_WATCH_DIR}" 2>/dev/null; then
        echo "WARNING: Could not fix watch directory permissions"
        echo "         The watcher may not be able to index files"
        echo "         For Podman, use --userns=keep-id flag"
    fi
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
