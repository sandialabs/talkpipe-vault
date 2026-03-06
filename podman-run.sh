#!/bin/bash
# Script to run TalkPipe Vault in a Podman container
# This script mounts directories from the user's desktop

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=podman-config.sh
source "${SCRIPT_DIR}/podman-config.sh"

# Create directories if they don't exist
mkdir -p "${DATA_DIR}/vault"
mkdir -p "${DATA_DIR}/watch"

# Check if image exists, if not, build it
if ! podman image exists "${IMAGE_NAME}" 2>/dev/null; then
    "${SCRIPT_DIR}/podman-build.sh"
fi

# Run the container
echo "Starting TalkPipe Vault container..."
echo "  Data directory: ${DATA_DIR}"
echo "  Web interface: http://localhost:8002"

podman run -it --rm \
    --userns=keep-id \
    --network host \
    -v "${DATA_DIR}:/app/data:Z" \
    -e VAULT_HOST=0.0.0.0 \
    -e VAULT_PORT=8002 \
    "${IMAGE_NAME}"
