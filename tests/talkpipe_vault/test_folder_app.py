"""Tests for folder mode in the web application and the vault-server CLI."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talkpipe_vault.apps import query, user_settings, vault_server
from talkpipe_vault.pipelines.config import FOLDER_INDEX_DIRNAME
from tests.conftest import FAKE_EMBEDDING_SOURCE


@pytest.fixture(autouse=True)
def folder_app(tmp_path, monkeypatch, fake_embedder):
    """A clean app state using the offline embedder and the eliza responder."""
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "vault-home"))
    monkeypatch.delenv("TALKPIPE_VAULT_ROOT", raising=False)
    monkeypatch.delenv("TALKPIPE_DOCUMENT_ROOTS", raising=False)
    user_settings.save_model_overrides(
        embedding_source=FAKE_EMBEDDING_SOURCE,
        embedding_model="fake",
        chat_source="eliza",
        chat_model="eliza",
    )
    query.load_saved_model_overrides()
    query._state.vault_path = ""
    query._state.folders = []
    query._state.show_source_paths = False
    query._state.last_refresh_time = 0.0
    query._clear_pipelines(query._state)
    yield fake_embedder
    if query._folder_scheduler is not None:
        query._folder_scheduler.stop()
        query._folder_scheduler = None
    query._state.folders = []
    query._state.vault_path = ""
    query._clear_pipelines(query._state)


@pytest.fixture
def client():
    return TestClient(query.app)


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "lighthouse.txt").write_text(
        "The lighthouse keeper logged a storm off the northern reef.\n"
    )
    (root / "sub" / "garden.md").write_text(
        "Garden journal: basil, tomatoes and the old greenhouse.\n"
    )
    return root


def _wait(predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _complete():
    statuses = query.folders_status(query._state)
    return bool(statuses) and all(s["complete"] for s in statuses)


def _searchable_by_keyword():
    statuses = query.folders_status(query._state)
    return bool(statuses) and all(
        s["files_total"] and s["files_keyword"] == s["files_total"] for s in statuses
    )


def test_adding_a_folder_searches_it_in_place(client, docs):
    response = client.post(
        "/folders/add", data={"folder_path": str(docs)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "error" not in response.headers["location"]
    root = str(docs.resolve())
    assert query._state.folders == [root]
    assert query._state.vault_path == str(docs.resolve() / FOLDER_INDEX_DIRNAME)
    assert _wait(_complete)
    assert user_settings.get_open_folders() == [root]
    assert user_settings.get_last_session() == user_settings.SESSION_FOLDERS

    status = client.get("/api/index-status").json()
    [folder] = status["folders"]
    assert folder["root"] == root
    assert folder["progress"].startswith("Ready · 2 files")
    assert status["banner"] == ""

    page = client.get("/documents").text
    assert "lighthouse" not in page or root in page
    assert "Ready · 2 files" in page
    assert "Named vaults (advanced)" in page


def test_search_ask_and_lookups_work_over_a_folder(client, docs):
    query.add_folder(query._state, str(docs))
    assert _wait(_complete)
    query._refresh_pipelines(force=True)

    page = client.post("/search", data={"query": "lighthouse storm"}).text
    assert "lighthouse.txt" in page

    [hit] = query._process_semantic_results(
        query._state.search_pipeline("lighthouse storm")
    )[:1]
    key = hit["lookup_path"]
    chunk = client.get("/chunk-content", params={"path": key})
    assert chunk.status_code == 200
    assert "lighthouse keeper" in chunk.json()["content"]
    opened = client.get("/open-file", params={"path": key})
    assert opened.status_code == 200
    assert "northern reef" in opened.text

    answer = client.post("/chat", data={"message": "What did the keeper log?"})
    assert answer.status_code == 200


def test_unfinished_folder_is_flagged_and_still_answers(client, docs, folder_app):
    folder_app["fail_after"] = 0  # the embedder is down: keywords only
    query.add_folder(query._state, str(docs))
    assert _wait(_searchable_by_keyword)
    query._refresh_pipelines(force=True)

    banner = query._folder_banner(query._state)
    assert banner.startswith("Still indexing docs (keywords 100%, meaning 0%)")
    assert "partial index: docs 0% embedded" in query._partial_index_note(query._state)
    page = client.post("/search", data={"query": "lighthouse storm"}).text
    assert "lighthouse.txt" in page  # found by keyword, not yet embedded
    assert "Still indexing docs" in page
    answer = client.post("/chat", data={"message": "What did the keeper log?"}).text
    assert "partial index: docs 0% embedded" in answer


def test_nested_folders_are_covered_once(client, docs):
    query.add_folder(query._state, str(docs / "sub"))
    _, error = query.add_folder(query._state, str(docs / "sub" / "."))
    assert error == ""  # the same folder again is just a note
    message, error = query.add_folder(query._state, str(docs))
    assert error == ""
    assert "replaces" in message
    assert query._state.folders == [str(docs.resolve())]
    _, error = query.add_folder(query._state, str(docs / "sub"))
    assert "already covered" in error


def test_removing_a_folder_can_delete_its_index_only(client, docs):
    query.add_folder(query._state, str(docs))
    assert _wait(_searchable_by_keyword)
    index = docs / FOLDER_INDEX_DIRNAME
    assert index.is_dir()
    response = client.post(
        "/folders/remove",
        data={"root": str(docs.resolve()), "delete_index": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error" not in response.headers["location"]
    assert not index.exists()
    assert (docs / "lighthouse.txt").is_file()
    assert query._state.folders == []
    assert query._state.vault_path == ""


def test_named_vault_routes_refuse_a_folder_index(client, docs):
    query.add_folder(query._state, str(docs))
    response = client.post(
        "/documents/index", data={"source_path": str(docs)}, follow_redirects=False
    )
    assert "Folders+index+themselves" in response.headers["location"]
    response = client.post("/keyword-search/create-index", follow_redirects=False)
    assert "build+their+keyword+index" in response.headers["location"]


def test_opening_a_named_vault_leaves_folder_mode(client, docs, tmp_path):
    query.add_folder(query._state, str(docs))
    vault = tmp_path / "my-vault"
    response = client.post(
        "/vaults/open", data={"new_vault_path": str(vault)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert query._state.folders == []
    assert query._state.vault_path == str(vault)
    assert user_settings.get_last_session() == user_settings.SESSION_VAULT
    # The folder is remembered for "Searched before".
    assert str(docs.resolve()) in user_settings.get_recent_folders()


def test_opening_a_searched_folder_as_a_vault_opens_the_folder(client, docs):
    query.add_folder(query._state, str(docs))
    assert _wait(_searchable_by_keyword)
    root = str(docs.resolve())
    query.remove_folder(query._state, root)
    client.post("/vaults/open", data={"new_vault_path": str(docs)})
    assert query._state.folders == [root]


# -- vault-server ----------------------------------------------------------------


@pytest.fixture
def captured_run(monkeypatch):
    monkeypatch.setattr(vault_server, "_running_instance_url", lambda h, p: None)
    monkeypatch.setattr(vault_server, "_port_in_use", lambda h, p: False)
    calls = {}
    monkeypatch.setattr(vault_server, "run_app", lambda **k: calls.update(k))
    return calls


def test_cli_opens_a_documents_folder_in_folder_mode(monkeypatch, docs, captured_run):
    monkeypatch.setattr("sys.argv", ["vault-server", str(docs), "--no-browser"])
    vault_server.main()
    assert captured_run["folders"] == [str(docs)]
    assert captured_run["vault_path"] == ""


def test_cli_as_vault_keeps_the_old_behavior(monkeypatch, docs, captured_run):
    monkeypatch.setattr(
        "sys.argv", ["vault-server", str(docs), "--as-vault", "--no-browser"]
    )
    vault_server.main()
    assert captured_run["folders"] == []
    assert captured_run["vault_path"] == str(docs)


def test_cli_dir_flags_and_an_empty_vault_folder(
    monkeypatch, docs, tmp_path, captured_run
):
    vault = tmp_path / "new-vault"
    monkeypatch.setattr(
        "sys.argv",
        ["vault-server", str(vault), "--dir", str(docs), "--dir", str(docs / "sub")],
    )
    vault_server.main()
    assert captured_run["folders"] == [str(docs), str(docs / "sub")]


def test_cli_resume_reopens_the_last_folders(monkeypatch, docs, captured_run):
    user_settings.set_open_folders([str(Path(docs).resolve())])
    user_settings.set_last_session(user_settings.SESSION_FOLDERS)
    monkeypatch.setattr("sys.argv", ["vault-server", "--resume", "--no-browser"])
    vault_server.main()
    assert captured_run["folders"] == [str(Path(docs).resolve())]
