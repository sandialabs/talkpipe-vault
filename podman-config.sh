#!/bin/bash
# Shared configuration for podman scripts
# Source this from podman-build.sh, podman-run.sh, and podman-shell.sh

IMAGE_NAME="${IMAGE_NAME:-talkpipe-vault}"
DESKTOP_DIR="${DESKTOP_DIR:-${HOME}/Desktop}"
VAULT_DIR="${VAULT_DIR:-${DESKTOP_DIR}/vault}"
WATCH_DIR="${WATCH_DIR:-${DESKTOP_DIR}/watch}"
