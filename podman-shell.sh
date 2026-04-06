#!/bin/bash
# Script to connect to a running TalkPipe Vault container for testing and debugging

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=podman-config.sh
source "${SCRIPT_DIR}/podman-config.sh"

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

# Use baked-in test script from image
TESTS_SH="/app/talkpipe_tests.sh"

# Handle command-line arguments for running tests non-interactively
case "${1:-}" in
    --test|-t)
        echo "Running all tests..."
        podman exec "${CONTAINER_ID}" /bin/bash -c "source ${TESTS_SH} && test-all"
        exit 0
        ;;
    --test-network|-n)
        podman exec "${CONTAINER_ID}" /bin/bash -c "source ${TESTS_SH} && test_network"
        exit 0
        ;;
    --test-ollama|-o)
        podman exec "${CONTAINER_ID}" /bin/bash -c "source ${TESTS_SH} && test_ollama"
        exit 0
        ;;
    "")
        ;;
    *)
        echo "Unknown option: $1"
        exit 1
        ;;
esac

# Interactive mode - open shell
echo "Opening interactive shell with test functions loaded..."
echo ""
echo "Available test commands:"
echo "  test_network  or  test-net   - Test network connectivity"
echo "  test_ollama   or  test-ollama - Test Ollama connectivity"
echo "  test-all                      - Run all tests"
echo ""
echo "Type 'exit' to leave the container shell."
echo ""
podman exec -it "${CONTAINER_ID}" /bin/bash --init-file "${TESTS_SH}"
