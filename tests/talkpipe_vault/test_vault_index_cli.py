"""Tests for the ``vault-index`` command (apps/vault_index.py)."""

import json

import pytest

from talkpipe_vault.apps import user_settings, vault_index
from talkpipe_vault.pipelines import vault_metadata
from talkpipe_vault.pipelines.config import FOLDER_INDEX_DIRNAME
from tests.conftest import FAKE_EMBEDDING_SOURCE


@pytest.fixture(autouse=True)
def vault_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TALKPIPE_VAULT_HOME", str(home))
    monkeypatch.delenv("TALKPIPE_VAULT_ROOT", raising=False)
    return home


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "alpha.txt").write_text("The lighthouse keeper logged a storm.\n")
    (root / "sub" / "bravo.md").write_text("Garden journal: basil and tomatoes.\n")
    return root


def test_indexes_a_folder_and_summarizes_it(docs, fake_embedder, capsys):
    code = vault_index.main(
        [
            str(docs),
            "--embedding-source",
            FAKE_EMBEDDING_SOURCE,
            "--embedding-model",
            "fake",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip() == (
        f"{docs.resolve()}: 2 files, 2 keyword-indexed, 2 embedded "
        f"({FAKE_EMBEDDING_SOURCE}/fake)"
    )
    recorded = vault_metadata.load_embedding_config(docs / FOLDER_INDEX_DIRNAME)
    assert (recorded["source"], recorded["model"]) == (FAKE_EMBEDDING_SOURCE, "fake")


def test_keywords_only_and_json_output(docs, fake_embedder, capsys):
    code = vault_index.main([str(docs), "--keywords-only", "--json"])
    status = json.loads(capsys.readouterr().out)
    assert code == 0
    assert status["files_keyword"] == 2
    assert status["files_embedded"] == 0
    assert fake_embedder["calls"] == 0


def test_saved_settings_are_the_defaults(docs, fake_embedder):
    user_settings.save_model_overrides(
        embedding_source=FAKE_EMBEDDING_SOURCE, embedding_model="fake"
    )
    assert vault_index.main([str(docs)]) == 0
    recorded = vault_metadata.load_embedding_config(docs / FOLDER_INDEX_DIRNAME)
    assert (recorded["source"], recorded["model"]) == (FAKE_EMBEDDING_SOURCE, "fake")


def test_a_missing_folder_fails_with_a_message(tmp_path, capsys):
    code = vault_index.main([str(tmp_path / "nope")])
    assert code == 1
    assert "is not a folder" in capsys.readouterr().err


def test_build_script_emits_only_the_options_given():
    script = vault_index.build_script(
        embedding_source="ollama",
        embedding_model=None,
        index_location="auto",
        chunk_size=None,
        shingle_size=3,
        overlap=None,
        vectors=False,
    )
    assert script == (
        '| indexFolder[index_location="auto", embedding_source="ollama", '
        "shingle_size=3, vectors=False]"
    )


def test_build_script_refuses_a_quote_in_a_value():
    with pytest.raises(SystemExit, match="embedding-model"):
        vault_index.build_script(
            embedding_source=None,
            embedding_model='x"y',
            index_location="auto",
            chunk_size=None,
            shingle_size=None,
            overlap=None,
            vectors=True,
        )


def _status(**overrides):
    status = {
        "root": "/docs",
        "role": "writer",
        "writer": "this process",
        "files_total": 1,
        "files_keyword": 1,
        "files_embedded": 1,
        "files_error": 0,
        "embedding": "model2vec/m",
        "error": "",
        "vector_error": "",
    }
    status.update(overrides)
    return status


def test_summary_counts_one_file_in_the_singular():
    assert vault_index.summarize(_status()) == (
        "/docs: 1 file, 1 keyword-indexed, 1 embedded (model2vec/m)"
    )


def test_summary_of_a_folder_another_process_keeps_up_to_date():
    # The other process may be idle (a vault-server watching a finished
    # folder), so the line must not claim it is indexing.
    line = vault_index.summarize(
        _status(role="reader", writer="another vault process (pid 7 on host)")
    )
    assert line == (
        "/docs: left alone, another vault process (pid 7 on host) keeps its "
        "index up to date"
    )
