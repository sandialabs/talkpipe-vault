#!/bin/bash
# Script to build the TalkPipe Vault container image

set -e

IMAGE_NAME="${IMAGE_NAME:-talkpipe-vault}"

echo "Building TalkPipe Vault container image: ${IMAGE_NAME}"

# Use Containerfile if it exists, otherwise Dockerfile
if [ -f Containerfile ]; then
    podman build -f Containerfile -t "${IMAGE_NAME}" .
elif [ -f Dockerfile ]; then
    podman build -f Dockerfile -t "${IMAGE_NAME}" .
else
    echo "Error: Neither Containerfile nor Dockerfile found"
    exit 1
fi

echo "Build complete! Image: ${IMAGE_NAME}"
echo "Run with: ./podman-run.sh"


