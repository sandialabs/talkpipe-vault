#!/bin/bash
# Script to build the TalkPipe Vault container image

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=podman-config.sh
source "${SCRIPT_DIR}/podman-config.sh"

echo "Building TalkPipe Vault container image: ${IMAGE_NAME}"

podman build -t "${IMAGE_NAME}" .

echo "Build complete! Image: ${IMAGE_NAME}"
echo "Run with: ./podman-run.sh"
