"""Unit tests for the per-vault embedding metadata sidecar."""

import json

from talkpipe_vault.pipelines import vault_metadata


def test_load_returns_none_when_absent(tmp_path):
    assert vault_metadata.load(str(tmp_path)) is None
    assert vault_metadata.load_embedding_config(str(tmp_path)) is None


def test_record_then_load_round_trip(tmp_path):
    vault_metadata.record_embedding_config(
        str(tmp_path),
        source="model2vec",
        model="minishlab/potion-retrieval-32M",
        dimension=512,
        retrieval_template="task: search result | query: {query}",
    )
    embedding = vault_metadata.load_embedding_config(str(tmp_path))
    assert embedding is not None
    assert embedding["source"] == "model2vec"
    assert embedding["model"] == "minishlab/potion-retrieval-32M"
    assert embedding["dimension"] == 512
    assert embedding["retrieval_template"] == "task: search result | query: {query}"


def test_record_stores_url_only_as_breadcrumb(tmp_path):
    vault_metadata.record_embedding_config(
        str(tmp_path),
        source="ollama",
        model="nomic-embed-text",
        server_url="http://example.test:11434",
    )
    embedding = vault_metadata.load_embedding_config(str(tmp_path))
    assert embedding["indexed_via_url"] == "http://example.test:11434"


def test_record_omits_optional_fields_when_not_given(tmp_path):
    vault_metadata.record_embedding_config(str(tmp_path), source="model2vec", model="m")
    embedding = vault_metadata.load_embedding_config(str(tmp_path))
    assert "dimension" not in embedding
    assert "indexed_via_url" not in embedding
    assert "retrieval_template" not in embedding


def test_load_embedding_config_rejects_partial_record(tmp_path):
    # A record missing source/model is treated as "no record" (legacy).
    path = tmp_path / vault_metadata.METADATA_FILENAME
    path.write_text(json.dumps({"version": 1, "embedding": {"source": "model2vec"}}))
    assert vault_metadata.load_embedding_config(str(tmp_path)) is None


def test_load_survives_corrupt_file(tmp_path):
    path = tmp_path / vault_metadata.METADATA_FILENAME
    path.write_text("{ not json")
    assert vault_metadata.load(str(tmp_path)) is None
    assert vault_metadata.load_embedding_config(str(tmp_path)) is None


def test_probe_embedding_dimension_uses_registered_adapter(monkeypatch):
    from talkpipe.llm import config as llm_config

    class _FakeEmbeddingAdapter:
        def __init__(self, model=None):
            self.model = model

        def execute_one(self, text):
            return [0.0] * 7

    llm_config.registerEmbeddingAdapter("dimprobe", _FakeEmbeddingAdapter)
    try:
        assert vault_metadata.probe_embedding_dimension("dimprobe", "m") == 7
    finally:
        llm_config._embeddingAdapter.pop("dimprobe", None)


def test_probe_embedding_dimension_none_for_unknown_source():
    assert vault_metadata.probe_embedding_dimension("no-such-source", "m") is None
