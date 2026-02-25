#!/bin/bash
set -e

# Default values
VAULT_PATH="${VAULT_PATH:-/vault}"
VAULT_WATCH_DIR="${VAULT_WATCH_DIR:-/watch}"
VAULT_HOST="${VAULT_HOST:-0.0.0.0}"
VAULT_PORT="${VAULT_PORT:-8002}"

# Print permission error for a directory
permission_error() {
    local dir="$1"
    local name="$2"
    local extra="${3:-}"
    echo "ERROR: Cannot create files in ${dir}"
    echo "       Current user: $(id -u):$(id -g)"
    echo "       Directory owner: $(stat -c '%U:%G (%u:%g)' "${dir}" 2>/dev/null || echo 'unknown')"
    echo "       Directory permissions: $(stat -c '%a' "${dir}" 2>/dev/null || echo 'unknown')"
    [ -n "$extra" ] && echo "$extra"
}

# Verify a directory exists, is writable, and we can create files in it
check_writable() {
    local dir="$1"
    local name="$2"
    local test_file="${dir}/.write_test_$(date +%s)"

    if ! mkdir -p "${dir}" 2>/dev/null; then
        echo "ERROR: Cannot create ${dir}"
        echo "       Check that the vault directory is writable by the container user"
        echo "       For Podman, use --userns=keep-id flag"
        exit 1
    fi

    if ! touch "$test_file" 2>/dev/null; then
        permission_error "$dir" "$name" "$3"
        rm -f "$test_file" 2>/dev/null || true
        exit 1
    fi
    rm -f "$test_file" 2>/dev/null || true
}

# Clean up stale Whoosh lock files
cleanup_lock_files() {
    local dir="$1"
    local lockfile

    find "${dir}" -name "*WRITELOCK" -type f -delete 2>/dev/null || true
    find "${dir}" -name "*READLOCK" -type f -delete 2>/dev/null || true

    local lock_files
    lock_files=$(find "${dir}" -name "*LOCK" -type f 2>/dev/null || true)
    if [ -n "$lock_files" ]; then
        echo "Found lock files that couldn't be deleted automatically, attempting to fix permissions..."
        while read -r lockfile; do
            [ -f "$lockfile" ] || continue
            chmod 666 "$lockfile" 2>/dev/null || true
            rm -f "$lockfile" 2>/dev/null || true
        done <<< "$lock_files"

        local remaining
        remaining=$(find "${dir}" -name "*LOCK" -type f 2>/dev/null || true)
        if [ -n "$remaining" ]; then
            echo "WARNING: Could not remove all lock files. They may cause permission errors."
            echo "         Remaining lock files:"
            while read -r lockfile; do
                echo "           $lockfile (owner: $(stat -c '%U:%G' "$lockfile" 2>/dev/null || echo 'unknown'))"
            done <<< "$remaining"
            echo "         You may need to manually remove them or fix permissions on the host."
        else
            echo "Cleaned up stale lock files"
        fi
    else
        echo "Cleaned up stale lock files"
    fi
}

echo "Starting TalkPipe Vault..."
echo "  Vault storage: ${VAULT_PATH}"
echo "  Watch directory: ${VAULT_WATCH_DIR}"
echo "  Web server: ${VAULT_HOST}:${VAULT_PORT}"

# Ensure vault directory exists and has correct permissions
echo "Setting up vault directory..."
chmod -R u+rwX "${VAULT_PATH}" 2>/dev/null || true

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

FULLTEXT_EXTRA="
This is why Whoosh cannot create lock files. The directory exists but is not writable.

Solutions:
  For Podman with docker-compose:
    export UID=\$(id -u)
    export GID=\$(id -g)
    podman compose down
    podman compose up -d

  For Podman direct run: Use --userns=keep-id flag

  Fix permissions on host:
    sudo chown -R \$(id -u):\$(id -g) $(realpath ${VAULT_PATH})
    chmod -R u+rwX $(realpath ${VAULT_PATH})"

check_writable "${VAULT_PATH}/fulltext_vault" "fulltext_vault" "$FULLTEXT_EXTRA"
check_writable "${VAULT_PATH}/vector_vault" "vector_vault" "Fix permissions on host: chmod -R u+rwX $(realpath ${VAULT_PATH}/vector_vault)"

# Clean up any stale Whoosh lock files that might cause permission errors
echo "Checking for stale lock files..."
if [ -d "${VAULT_PATH}/fulltext_vault" ]; then
    cleanup_lock_files "${VAULT_PATH}/fulltext_vault"
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
