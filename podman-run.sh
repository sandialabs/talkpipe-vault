#!/bin/bash
# Script to run TalkPipe Vault in a Podman container
# This script mounts directories from the user's desktop

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=podman-config.sh
source "${SCRIPT_DIR}/podman-config.sh"

# Create directories if they don't exist
mkdir -p "${VAULT_DIR}"
mkdir -p "${WATCH_DIR}"

# Check if image exists, if not, build it
if ! podman image exists "${IMAGE_NAME}" 2>/dev/null; then
    "${SCRIPT_DIR}/podman-build.sh"
fi

# Run the container
echo "Starting TalkPipe Vault container..."
echo "  Vault directory: ${VAULT_DIR}"
echo "  Watch directory: ${WATCH_DIR}"
echo "  Web interface: http://localhost:8002"

podman run -it --rm \
    --userns=keep-id \
    --network host \
    -v "${VAULT_DIR}:/vault:Z" \
    -v "${WATCH_DIR}:/watch:Z" \
    -e VAULT_PATH=/vault \
    -e VAULT_WATCH_DIR=/watch \
    -e VAULT_HOST=0.0.0.0 \
    -e VAULT_PORT=8002 \
    "${IMAGE_NAME}"
