"""Pytest configuration and fixtures for talkpipe-vault tests."""

import subprocess

import pytest


def is_ollama_available():
    """Check if ollama is available and running."""
    try:
        # Try to connect to ollama API (default port 11434)
        result = subprocess.run(
            ["curl", "-s", "http://localhost:11434/api/tags"],
            timeout=5,
            capture_output=True,
            text=True
        )
        # If curl is not available, try with python requests
        if result.returncode != 0:
            try:
                import requests
                response = requests.get("http://localhost:11434/api/tags", timeout=5)
                return response.status_code == 200
            except Exception:
                return False
        return result.returncode == 0
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Automatically skip Ollama-dependent tests if Ollama is not available."""
    ollama_available = is_ollama_available()

    if ollama_available:
        return

    for item in items:
        path = str(item.fspath)
        # Skip pipelines tests (embedding/chat) when Ollama isn't running
        if "/pipelines/" in path:
            item.add_marker(
                pytest.mark.skip(
                    reason="Ollama is not available. Start Ollama (localhost:11434) to run pipelines tests."
                )
            )
        # Also skip searching_and_prompting specifically if matched by filename
        elif "searching_and_prompting" in path:
            item.add_marker(
                pytest.mark.skip(
                    reason="Ollama is not available. Start Ollama (localhost:11434) to run these tests."
                )
            )
