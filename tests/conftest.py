"""Pytest configuration and fixtures for talkpipe-vault tests."""

import subprocess
import sys

import pytest

DEFAULT_OLLAMA_URL = "http://localhost:11434"


def build_docs_vault(source_glob, vault_path):
    """Build a docs-table vault with talkpipe's makevectordatabase CLI.

    talkpipe 0.12.4 requires explicit embedding configuration, so the
    configured (or default) vault embedding model/source is passed through.
    """
    from talkpipe_vault.pipelines.config import (
        get_embedding_model,
        get_embedding_source,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "talkpipe.app.makevectordatabase",
            str(source_glob),
            "--path",
            str(vault_path),
            "--embedding_source",
            get_embedding_source(),
            "--embedding_model",
            get_embedding_model(),
            "--overwrite",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return str(vault_path)


def get_ollama_url():
    """Return the Ollama server URL from TalkPipe config, or the default."""
    try:
        from talkpipe.util.config import get_config
        from talkpipe.util.constants import OLLAMA_SERVER_URL

        configured = get_config().get(OLLAMA_SERVER_URL)
    except Exception:
        configured = None
    return (configured or DEFAULT_OLLAMA_URL).rstrip("/")


def is_ollama_available():
    """Check if the configured Ollama server is reachable."""
    try:
        import requests

        response = requests.get(f"{get_ollama_url()}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Automatically skip Ollama-dependent tests if Ollama is not available."""
    ollama_available = is_ollama_available()

    if ollama_available:
        return

    skip_reason = (
        f"Ollama is not available at {get_ollama_url()}. Start Ollama or point "
        "TALKPIPE_OLLAMA_SERVER_URL at a reachable server to run these tests."
    )
    for item in items:
        path = str(item.fspath)
        # Skip pipelines tests (embedding/chat) when Ollama isn't running
        if "/pipelines/" in path or "searching_and_prompting" in path:
            item.add_marker(pytest.mark.skip(reason=skip_reason))


FAKE_EMBEDDING_SOURCE = "fakehash"
FAKE_EMBEDDING_DIM = 32


def _fake_hash_vector(text):
    """Deterministic hashed bag-of-words vector, unit length (offline embedder)."""
    import hashlib
    import math

    vector = [0.0] * FAKE_EMBEDDING_DIM
    for word in str(text).lower().split():
        digest = hashlib.sha1(word.encode("utf-8")).digest()
        vector[digest[0] % FAKE_EMBEDDING_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    if not any(vector):
        vector[0] = 1.0
    return [v / norm for v in vector]


@pytest.fixture
def fake_embedder():
    """Register the offline 'fakehash' embedding source for one test.

    Yields a controller: set ``fail_after`` to make every embedding after
    that many calls raise, and read ``calls``.
    """
    from talkpipe.llm import config as llm_config

    control = {"calls": 0, "fail_after": None}

    class _FakeHashEmbedder:
        def __init__(self, model=None, *args, **kwargs):
            self.model = model

        def execute_one(self, text):
            control["calls"] += 1
            if (
                control["fail_after"] is not None
                and control["calls"] > control["fail_after"]
            ):
                raise ConnectionError("fake embedder is down")
            return _fake_hash_vector(text)

        def execute_batch(self, texts):
            return [self.execute_one(text) for text in texts]

    llm_config.registerEmbeddingAdapter(FAKE_EMBEDDING_SOURCE, _FakeHashEmbedder)
    try:
        yield control
    finally:
        llm_config._embeddingAdapter.pop(FAKE_EMBEDDING_SOURCE, None)
