"""Per-vault metadata sidecar (embedding provenance).

Records the embedding configuration a vault was indexed with, so reopening the
vault can restore the embedder that produced its vectors. Embeddings are only
comparable to a query embedded with the *same* model, so this is a correctness
record of what the data was made with — embedding source, model, and dimension —
not a user preference.

Deliberately excluded: server URLs and API keys. Those describe how to reach a
service, not the data (the same model on a different host produces the same
vectors), and baking them into a vault that may be copied or shared is a
portability footgun. A server URL is kept only as a non-authoritative
``indexed_via_url`` breadcrumb for traceability; the live URL and any API key are
always resolved fresh at run time.

The record lives at ``<vault_path>/vault_metadata.json`` so it travels with the
vault directory if it is copied, moved, or shared.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

METADATA_FILENAME = "vault_metadata.json"
SCHEMA_VERSION = 1


def _metadata_path(vault_path: str) -> Path:
    return Path(vault_path) / METADATA_FILENAME


def load(vault_path: str) -> dict[str, Any] | None:
    """Return the parsed sidecar, or None when it is absent or unreadable."""
    path = _metadata_path(vault_path)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Could not read vault metadata at %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def load_embedding_config(vault_path: str) -> dict[str, Any] | None:
    """Return the recorded embedding config, or None if absent/incomplete.

    A record missing the identifying ``source``/``model`` is treated as absent
    (legacy), so callers get a clean "no record" rather than a partial one.
    """
    data = load(vault_path)
    if not data:
        return None
    embedding = data.get("embedding")
    if not isinstance(embedding, dict):
        return None
    if not embedding.get("source") or not embedding.get("model"):
        return None
    return embedding


def record_embedding_config(
    vault_path: str,
    *,
    source: str,
    model: str,
    dimension: int | None = None,
    retrieval_template: str | None = None,
    server_url: str | None = None,
) -> None:
    """Write the embedding config a vault was indexed with (best effort).

    Failures are logged and swallowed: not being able to write the sidecar must
    never fail an otherwise-successful indexing run.
    """
    embedding: dict[str, Any] = {"source": source, "model": model}
    if dimension is not None:
        embedding["dimension"] = dimension
    if retrieval_template is not None:
        embedding["retrieval_template"] = retrieval_template
    if server_url:
        # Traceability breadcrumb only — never applied on open.
        embedding["indexed_via_url"] = server_url
    _write(vault_path, {"version": SCHEMA_VERSION, "embedding": embedding})


def probe_embedding_dimension(source: str, model: str) -> int | None:
    """Return the vector dimension this embedder produces, or None on failure.

    Builds the same adapter used for indexing and embeds a tiny probe string.
    Best effort: any failure (unknown source, missing package, network error)
    returns None so it never blocks indexing.
    """
    try:
        from talkpipe.llm.config import getEmbeddingAdapter, getEmbeddingSources

        if source not in getEmbeddingSources():
            return None
        vector = getEmbeddingAdapter(source)(model=model).execute_one(
            "vault embedding dimension probe"
        )
        return len(vector) if vector else None
    except Exception as exc:
        logger.debug(
            "Could not determine embedding dimension for %s/%s: %s",
            source,
            model,
            exc,
        )
        return None


def _write(vault_path: str, payload: dict[str, Any]) -> None:
    path = _metadata_path(vault_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp_path.replace(path)
    except OSError as exc:
        logger.warning("Could not write vault metadata at %s: %s", path, exc)
