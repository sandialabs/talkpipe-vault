#!/bin/bash
# Shared configuration for podman scripts
# Source this from podman-build.sh, podman-run.sh, and podman-shell.sh

IMAGE_NAME="${IMAGE_NAME:-talkpipe-vault}"
DESKTOP_DIR="${DESKTOP_DIR:-${HOME}/Desktop}"
DATA_DIR="${DATA_DIR:-${DESKTOP_DIR}/talkpipe-data}"
VAULT_DIR="${DATA_DIR}/vault"
WATCH_DIR="${DATA_DIR}/watch"
