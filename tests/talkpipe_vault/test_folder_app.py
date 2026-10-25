"""Tests for folder mode in the web application and the vault-server CLI."""

import html
import re
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
    """Every folder finished, or one failed (asserted, so the test stops)."""
    statuses = query.folders_status(query._state)
    for status in statuses:
        failure = status["error"] or status["vector_error"]
        assert not failure, failure
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


def test_the_documents_page_leads_with_folders(client, tmp_path):
    # Named vaults stay closed, even for someone who used one before, and
    # Add folder waits for a folder to be chosen (the page's script enables
    # it once the picker can open the path).
    user_settings.remember_vault(str(tmp_path / "old-vault"))
    page = client.get("/documents").text
    assert re.search(r'<details class="named-vaults" id="named-vaults"\s*>', page)
    assert 'id="folder-add-btn" class="btn-secondary" disabled' in page
    assert 'id="folder-browse-btn">' in page


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


def test_results_name_their_folder_when_several_are_open(client, docs, tmp_path):
    page = client.get("/documents").text
    assert ".flash-status[hidden]" in page  # an empty banner stays hidden
    query.add_folder(query._state, str(docs))
    assert _wait(_complete)
    query._refresh_pipelines(force=True)
    one = client.post("/search", data={"query": "lighthouse storm"}).text
    assert "lighthouse.txt — " not in one  # one folder: nothing to tell apart

    other = tmp_path / "other"
    other.mkdir()
    (other / "lighthouse.txt").write_text("A second lighthouse, on the south cape.\n")
    query.add_folder(query._state, str(other))
    assert _wait(_complete)
    query._refresh_pipelines(force=True)
    page = client.post("/search", data={"query": "lighthouse"}).text
    assert "lighthouse.txt — docs" in page
    assert "lighthouse.txt — other" in page
    keywords = client.post("/keyword-search", data={"query": "lighthouse"}).text
    assert "lighthouse.txt — other" in keywords
    # Fused by rank, so the scores shown need not descend: the note says so.
    assert "a score compares only with others from the same folder" in keywords


def test_folder_progress_line_counts_and_watching():
    status = {
        "files_total": 1,
        "files_keyword": 1,
        "files_embedded": 1,
        "phase": "ready",
        "watch_mode": "live",
    }
    assert (
        query._folder_progress_line(status) == "Ready · 1 file · watching for changes"
    )
    status.update(files_total=3, files_keyword=3, files_embedded=3, watch_mode="rescan")
    assert query._folder_progress_line(status) == (
        "Ready · 3 files · checked for changes every 10 minutes"
    )


def test_unfinished_folder_is_flagged_and_still_answers(client, docs, folder_app):
    folder_app["fail_after"] = 0  # the embedder is down: keywords only
    query.add_folder(query._state, str(docs))
    assert _wait(_searchable_by_keyword)
    query._refresh_pipelines(force=True)

    banner = query._folder_banner(query._state)
    assert banner.startswith("Still indexing docs (keywords 100%, meaning 0%)")
    assert "partial index: docs 0% embedded" in query._partial_index_note(query._state)
    # Nothing embedded yet: semantic search waits, Ask answers from keywords.
    assert query.semantic_search_waiting(query._state)
    page = client.get("/search").text
    assert html.escape(query.SEMANTIC_SEARCH_WAITING, quote=False) in page
    assert 'id="search-btn" disabled' in page
    page = client.post("/search", data={"query": "lighthouse storm"}).text
    assert "lighthouse.txt" not in page
    assert "Still indexing docs" in page
    answer = client.post("/chat", data={"message": "What did the keeper log?"}).text
    assert "partial index: docs 0% embedded" in answer
    assert "lighthouse.txt" in answer  # cited from the keyword index


def test_ask_says_indexing_just_started_until_a_file_is_indexed(
    client, docs, monkeypatch
):
    query.add_folder(query._state, str(docs))
    real_status = query.folders_status
    scanning = {"phase": "scanning", "files_total": 0, "files_keyword": 0}
    monkeypatch.setattr(
        query,
        "folders_status",
        lambda state: [{**s, **scanning} for s in real_status(state)],
    )
    answer = client.post("/chat", data={"message": "What did the keeper log?"}).text
    assert html.escape(query.ASK_WAITING, quote=False) in answer
    assert 'id="server-answered-by"' not in answer
    # An empty folder whose scan finished is not "just started".
    scanning["phase"] = "ready"
    assert query.ask_waiting(query._state) == ""
    monkeypatch.setattr(query, "folders_status", real_status)
    assert _wait(_searchable_by_keyword)
    assert query.ask_waiting(query._state) == ""


def test_semantic_search_opens_once_a_folder_has_vectors(client, docs):
    query.add_folder(query._state, str(docs))
    assert _wait(_complete)
    assert query.semantic_search_waiting(query._state) == ""
    page = client.post("/search", data={"query": "lighthouse storm"}).text
    assert "Semantic search starts once" not in page
    assert "lighthouse.txt" in page


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


def test_adding_a_missing_folder_or_a_file_says_which(client, docs):
    _, error = query.add_folder(query._state, str(docs.parent / "dcos"))
    assert error.endswith("does not exist.")
    _, error = query.add_folder(query._state, str(docs / "lighthouse.txt"))
    assert error.endswith("is not a folder.")
    assert query._state.folders == []


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
    # The page says the folders were closed, and how to get back to them.
    location = html.unescape(response.headers["location"])
    assert "no+longer+searched" in location.replace("%20", "+")
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


def test_cli_dir_flags(monkeypatch, docs, captured_run):
    monkeypatch.setattr(
        "sys.argv",
        ["vault-server", "--dir", str(docs), "--dir", str(docs / "sub")],
    )
    vault_server.main()
    assert captured_run["folders"] == [str(docs), str(docs / "sub")]


def test_cli_refuses_a_vault_path_together_with_dir(
    monkeypatch, docs, tmp_path, captured_run, capsys
):
    # Folders and a named vault do not open together; the vault must not be
    # dropped silently (the same resolver serves vault-tui).
    vault = tmp_path / "new-vault"
    monkeypatch.setattr(
        "sys.argv", ["vault-server", str(vault), "--dir", str(docs), "--no-browser"]
    )
    with pytest.raises(SystemExit):
        vault_server.main()
    assert "--dir cannot be combined with a vault path" in capsys.readouterr().err
    assert not captured_run
    _, folders, _, error = query.startup_folders(
        str(vault), [str(docs)], resume=False, as_vault=False
    )
    assert folders == [str(docs)]
    assert error


def test_cli_resume_reopens_the_last_folders(monkeypatch, docs, captured_run):
    user_settings.set_open_folders([str(Path(docs).resolve())])
    user_settings.set_last_session(user_settings.SESSION_FOLDERS)
    monkeypatch.setattr("sys.argv", ["vault-server", "--resume", "--no-browser"])
    vault_server.main()
    assert captured_run["folders"] == [str(Path(docs).resolve())]


def test_cli_reopens_the_last_folders_by_default(monkeypatch, docs, captured_run):
    monkeypatch.setattr("sys.argv", ["vault-server", "--no-browser"])
    vault_server.main()
    assert captured_run["folders"] == []  # nothing opened yet: choose a folder
    assert captured_run["vault_path"] == ""
    user_settings.set_open_folders([str(Path(docs).resolve())])
    user_settings.set_last_session(user_settings.SESSION_FOLDERS)
    vault_server.main()
    assert captured_run["folders"] == [str(Path(docs).resolve())]
    # A last session on a named vault does not reopen its folders, nor the vault.
    user_settings.set_last_session(user_settings.SESSION_VAULT)
    vault_server.main()
    assert captured_run["folders"] == []
    assert captured_run["vault_path"] == ""
    # A path given on the command line wins over the last session.
    user_settings.set_last_session(user_settings.SESSION_FOLDERS)
    vault = docs.parent / "named"
    monkeypatch.setattr("sys.argv", ["vault-server", str(vault), "--no-browser"])
    vault_server.main()
    assert captured_run["folders"] == []
    assert captured_run["vault_path"] == str(vault)


# -- named vaults next to folder mode -------------------------------------------


def _named_vault(tmp_path, docs):
    vault = tmp_path / "named-vault"
    vault.mkdir()
    query.index_documents_into_vault(
        str(vault),
        str(docs / "**" / "*"),
        embedding_model="fake",
        embedding_source=FAKE_EMBEDDING_SOURCE,
    )
    return vault


def test_named_vault_from_one_folder_offers_the_folder(client, docs, tmp_path):
    vault = _named_vault(tmp_path, docs)
    query.init_pipelines(str(vault))
    assert query._vault_source_folder(query._state) == str(docs.resolve())
    page = client.get("/documents").text
    assert "Search the folder instead" in page


def test_a_second_run_on_the_same_named_vault_is_refused(tmp_path, docs):
    import filelock

    from talkpipe_vault.pipelines import folder_index

    vault = tmp_path / "named-vault"
    vault.mkdir()
    held = filelock.FileLock(
        str(vault / folder_index.WRITER_LOCK_FILENAME), is_singleton=False
    )
    held.acquire()
    try:
        query._run_index_job(
            str(vault),
            str(docs),
            str(docs / "**" / "*"),
            "fake",
            FAKE_EMBEDDING_SOURCE,
            300,
            3,
            1,
            False,
        )
    finally:
        held.release()
    assert "Another vault process is indexing this vault" in str(
        query._index_job_snapshot()["error"]
    )


def test_changing_the_embedder_points_folders_at_re_embed(client, docs):
    query.add_folder(query._state, str(docs))
    response = client.post(
        "/settings",
        data={"embedding_source": FAKE_EMBEDDING_SOURCE, "embedding_model": "fake-2"},
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert "Re-embed" in location
    assert "Overwrite" not in location


def test_the_first_folders_embedder_does_not_become_everyones(client, docs, tmp_path):
    from talkpipe_vault.pipelines import folder_index

    first = tmp_path / "first"
    first.mkdir()
    (first / "note.txt").write_text("Indexed earlier with another model.\n")
    folder_index.index_folder_now(
        folder_index.open_folder(
            first, embedding_source=FAKE_EMBEDDING_SOURCE, embedding_model="fake-2"
        )
    )
    query.add_folder(query._state, str(first))
    query.add_folder(query._state, str(docs))
    assert _wait(_complete)

    by_name = {s["name"]: s for s in query.folders_status(query._state)}
    # Settings say "fake": the folder made with fake-2 offers Re-embed, the
    # one indexed now with the Settings model does not.
    assert by_name["first"]["embedding_differs"]
    assert not by_name["docs"]["embedding_differs"]
    assert by_name["docs"]["embedding"] == f"{FAKE_EMBEDDING_SOURCE}/fake"


def test_open_file_serves_only_indexed_documents(client, docs):
    # Dotfiles are never indexed, so a request naming one must not be served
    # just because it lies under an open folder.
    (docs / ".env").write_text("SECRET=1\n")
    query.add_folder(query._state, str(docs))
    assert _wait(_complete)
    query._refresh_pipelines(force=True)
    assert (
        client.get("/open-file", params={"path": str(docs / ".env")}).status_code == 404
    )
    assert (
        client.get(
            "/open-file", params={"path": str(docs / "lighthouse.txt")}
        ).status_code
        == 404
    )
    [hit] = query._process_semantic_results(
        query._state.search_pipeline("lighthouse storm")
    )[:1]
    served = client.get("/open-file", params={"path": hit["lookup_path"]})
    assert served.status_code == 200
    assert b"lighthouse keeper" in served.content


def test_an_empty_folder_is_finished_not_still_indexing(client, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    query.add_folder(query._state, str(empty))
    assert _wait(
        lambda: all(s["phase"] == "ready" for s in query.folders_status(query._state))
    )
    assert query._folder_banner(query._state) == ""
    assert query._partial_index_note(query._state) == ""
    assert client.get("/api/index-status").json()["banner"] == ""


def test_adding_the_same_folder_twice_at_once_opens_it_once(docs):
    import threading

    outcomes = []
    threads = [
        threading.Thread(
            target=lambda: outcomes.append(query.add_folder(query._state, str(docs)))
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert query._state.folders == [str(docs.resolve())]
    assert len(query._open_folder_indexes(query._state)) == 1
    assert sum(1 for message, _ in outcomes if message.startswith("Searching")) == 1


def test_keyword_page_picks_up_the_folder_index_on_its_own(client, docs, monkeypatch):
    query.add_folder(query._state, str(docs))
    assert _wait(_searchable_by_keyword)
    # Stale: nothing has refreshed the pipelines since the index appeared.
    query._state.keyword_search_enabled = False
    query._state.last_refresh_time = 0.0
    page = client.get("/keyword-search").text
    assert "Keyword search starts shortly" not in page
    assert 'id="search-btn" disabled' not in page
    # Before the index exists, a search says it is coming, in folder terms.
    monkeypatch.setattr(query, "_refresh_pipelines", lambda force=False: None)
    query._state.keyword_search_enabled = False
    page = client.post("/keyword-search", data={"query": "lighthouse"}).text
    assert html.escape(query.KEYWORD_SEARCH_STARTING, quote=False) in page
    assert "no Whoosh index" not in page
