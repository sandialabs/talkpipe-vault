#!/bin/bash
# Script to connect to a running TalkPipe Vault container for testing and debugging

set -e

# Get the image name (defaults to talkpipe-vault)
IMAGE_NAME="${IMAGE_NAME:-talkpipe-vault}"

# Find running container by image name
CONTAINER_ID=$(podman ps --filter "ancestor=${IMAGE_NAME}" --format "{{.ID}}" | head -1)

if [ -z "$CONTAINER_ID" ]; then
    echo "Error: No running container found for image '${IMAGE_NAME}'"
    echo ""
    echo "To start a container, run:"
    echo "  ./podman-run.sh"
    exit 1
fi

CONTAINER_NAME=$(podman ps --filter "id=${CONTAINER_ID}" --format "{{.Names}}")

echo "Connecting to running container: ${CONTAINER_NAME} (${CONTAINER_ID:0:12})"
echo ""

# Copy test functions script into the container
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/talkpipe_tests.sh" ]; then
    podman cp "${SCRIPT_DIR}/talkpipe_tests.sh" "${CONTAINER_ID}:/tmp/talkpipe_tests.sh" > /dev/null
else
    echo "Error: talkpipe_tests.sh not found in ${SCRIPT_DIR}"
    exit 1
fi

# No need to create wrapper - we'll use --init-file directly

# Check for command-line arguments (for running tests non-interactively)
if [ "$1" = "--test" ] || [ "$1" = "-t" ]; then
    # Run all tests and exit
    echo "Running all tests..."
    podman exec "${CONTAINER_ID}" /bin/bash -c "source /tmp/talkpipe_tests.sh && test-all"
    exit 0
elif [ "$1" = "--test-network" ] || [ "$1" = "-n" ]; then
    podman exec "${CONTAINER_ID}" /bin/bash -c "source /tmp/talkpipe_tests.sh && test_network"
    exit 0
elif [ "$1" = "--test-ollama" ] || [ "$1" = "-o" ]; then
    podman exec "${CONTAINER_ID}" /bin/bash -c "source /tmp/talkpipe_tests.sh && test_ollama"
    exit 0
elif [ "$1" = "--test-tika" ] || [ "$1" = "-k" ]; then
    podman exec "${CONTAINER_ID}" /bin/bash -c "source /tmp/talkpipe_tests.sh && test_tika"
    exit 0
fi

# Interactive mode - open shell
echo "Opening interactive shell with test functions loaded..."
echo ""
echo "Available test commands:"
echo "  test_network  or  test-net   - Test network connectivity"
echo "  test_ollama   or  test-ollama - Test Ollama connectivity"
echo "  test_tika     or  test-tika   - Test Tika functionality"
echo "  test-all                      - Run all tests"
echo ""
echo "Type 'exit' to leave the container shell."
echo ""
# Start interactive bash with --init-file to load test functions
# --init-file is used for interactive shells (replaces ~/.bashrc)
podman exec -it "${CONTAINER_ID}" /bin/bash --init-file /tmp/talkpipe_tests.sh
