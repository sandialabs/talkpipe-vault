"""Unit tests for the pipelines config module."""

import os

import pytest

from talkpipe_vault.pipelines.config import (
    FULLTEXT_VAULT_SUBDIR,
    VECTOR_VAULT_SUBDIR,
    ensure_supported_vault_layout,
    get_vault_paths,
    get_vector_db_path,
    resolve_embedding_config,
)


class TestResolveEmbeddingConfig:
    """Tests for resolve_embedding_config."""

    def test_uses_explicit_values(self):
        """When both are provided, returns them as-is."""
        model, source = resolve_embedding_config("my-model", "my-source")
        assert model == "my-model"
        assert source == "my-source"

    def test_falls_back_for_none_model(self):
        """When model is None, uses config default."""
        model, source = resolve_embedding_config(None, "openai")
        assert model is not None
        assert source == "openai"

    def test_falls_back_for_none_source(self):
        """When source is None, uses config default."""
        model, source = resolve_embedding_config("embedding-3", None)
        assert model == "embedding-3"
        assert source is not None

    def test_falls_back_for_both_none(self):
        """When both are None, uses config defaults."""
        model, source = resolve_embedding_config(None, None)
        assert model is not None
        assert source is not None


class TestGetVaultPaths:
    """Tests for get_vault_paths."""

    def test_returns_expected_subdirs(self):
        """Paths use direct LanceDB path and FULLTEXT_VAULT_SUBDIR."""
        base = "/tmp/my_vault"
        vectordb_path, whoosh_path = get_vault_paths(base)
        assert vectordb_path == get_vector_db_path(base)
        assert whoosh_path == os.path.join(base, FULLTEXT_VAULT_SUBDIR)

    def test_subdir_constants(self):
        """Constants match expected values."""
        assert VECTOR_VAULT_SUBDIR == "vector_vault"
        assert FULLTEXT_VAULT_SUBDIR == "fulltext_vault"


class TestVaultLayoutValidation:
    """Tests for strict vault layout validation."""

    def test_rejects_legacy_nested_vector_layout(self, tmp_path):
        """Legacy vector_vault subdirectory should raise a migration error."""
        legacy_path = tmp_path / VECTOR_VAULT_SUBDIR
        legacy_path.mkdir(parents=True)

        with pytest.raises(ValueError, match="Unsupported legacy vault layout"):
            ensure_supported_vault_layout(str(tmp_path))

    def test_allows_direct_lancedb_layout(self, tmp_path):
        """Direct LanceDB path without vector_vault should pass validation."""
        ensure_supported_vault_layout(str(tmp_path))
