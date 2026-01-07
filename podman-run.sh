#!/bin/bash
# Script to run TalkPipe Vault in a Podman container
# This script mounts directories from the user's desktop

set -e

# Default values - adjust these to match your desktop paths
DESKTOP_DIR="${HOME}/Desktop"
VAULT_DIR="${DESKTOP_DIR}/vault"
WATCH_DIR="${DESKTOP_DIR}/watch"

# Create directories if they don't exist
mkdir -p "${VAULT_DIR}"
mkdir -p "${WATCH_DIR}"

# Get the image name (defaults to talkpipe-vault)
IMAGE_NAME="${IMAGE_NAME:-talkpipe-vault}"

# Check if image exists, if not, build it
if ! podman image exists "${IMAGE_NAME}" 2>/dev/null; then
    echo "Image ${IMAGE_NAME} not found. Building..."
    podman build -t "${IMAGE_NAME}" .
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


