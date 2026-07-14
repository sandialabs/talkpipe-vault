"""Tests for the experimental watcher CLI helpers."""

import sys

import pytest

from talkpipe_vault.pipelines.cli import watch_vectordb_main


def test_watch_vectordb_main_rejects_missing_source_path(tmp_path, monkeypatch, capsys):
    """A bad watch path must fail cleanly before any vault scaffolding is written."""
    vault_path = tmp_path / "vault"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "watch_vectordb_main",
            "/path/that/does/not/exist",
            "--vault-path",
            str(vault_path),
            "--polling",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        watch_vectordb_main()

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "/path/that/does/not/exist" in stderr
    assert "does not exist" in stderr
    # The vault directory must not have been created for a rejected run.
    assert not vault_path.exists()
