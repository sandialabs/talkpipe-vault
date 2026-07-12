"""Tests for vault management, document indexing, and model settings pages."""

import pytest
from fastapi.testclient import TestClient

from talkpipe_vault.apps import query, user_settings


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    """Isolate app state and persisted settings for each test."""
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "vault-home"))
    with query._index_job_lock:
        query._index_job.running = False
        query._index_job.source = ""
        query._index_job.vault_path = ""
        query._index_job.total_files = 0
        query._index_job.files_done = 0
        query._index_job.current_file = ""
        query._index_job.chunks = 0
        query._index_job.message = ""
        query._index_job.error = None
    query._state.vault_path = ""
    query._state.search_pipeline = None
    query._state.chat_pipeline = None
    query._state.keyword_search_pipeline = None
    query._state.shingled_chunks_count = 0
    query._state.fulltext_documents_count = 0
    query._state.keyword_search_enabled = False
    query._state.show_source_paths = False
    query._state.last_refresh_time = 0.0
    query._state.embedding_model = None
    query._state.embedding_source = None
    query._state.chat_model = None
    query._state.chat_source = None
    yield


@pytest.fixture
def client():
    return TestClient(query.app, follow_redirects=False)


class TestUserSettings:
    """Unit tests for the persisted settings module."""

    def test_recent_vaults_ordered_and_deduplicated(self):
        user_settings.remember_vault("/vault/a")
        user_settings.remember_vault("/vault/b")
        user_settings.remember_vault("/vault/a")

        assert user_settings.get_recent_vaults() == ["/vault/a", "/vault/b"]

    def test_recent_vaults_capped(self):
        for i in range(user_settings.MAX_RECENT_VAULTS + 5):
            user_settings.remember_vault(f"/vault/{i}")

        assert len(user_settings.get_recent_vaults()) == (
            user_settings.MAX_RECENT_VAULTS
        )

    def test_model_overrides_roundtrip(self):
        user_settings.save_model_overrides(
            embedding_source="openai",
            embedding_model="text-embedding-3-large",
        )

        overrides = user_settings.get_model_overrides()
        assert overrides == {
            "embedding_source": "openai",
            "embedding_model": "text-embedding-3-large",
        }

    def test_blank_override_clears_saved_value(self):
        user_settings.save_model_overrides(chat_model="mistral-small")
        user_settings.save_model_overrides(chat_model="")

        assert "chat_model" not in user_settings.get_model_overrides()

    def test_unreadable_settings_file_is_ignored(self):
        home = user_settings.get_vault_home()
        home.mkdir(parents=True)
        (home / user_settings.SETTINGS_FILENAME).write_text("not json")

        assert user_settings.get_recent_vaults() == []


class TestVaultSelection:
    """Tests for choosing and creating vaults from the interface."""

    def test_home_redirects_to_vault_manager_without_vault(self, client):
        response = client.get("/")

        assert response.status_code == 303
        assert response.headers["location"].startswith("/vaults")

    def test_search_pages_redirect_without_vault(self, client):
        for path in ("/search", "/keyword-search", "/chat", "/documents"):
            response = client.get(path)
            assert response.status_code == 303, path
            assert response.headers["location"].startswith("/vaults"), path

    def test_vaults_page_lists_recent_vaults(self, client):
        user_settings.remember_vault("/vault/alpha")

        response = client.get("/vaults")

        assert response.status_code == 200
        assert "/vault/alpha" in response.text
        assert 'action="/vaults/open"' in response.text

    def test_open_creates_new_vault_and_remembers_it(self, client, tmp_path):
        new_vault = tmp_path / "brand-new-vault"

        response = client.post("/vaults/open", data={"new_vault_path": str(new_vault)})

        assert response.status_code == 303
        assert response.headers["location"].startswith("/documents")
        assert new_vault.is_dir()
        assert query._state.vault_path == str(new_vault)
        assert user_settings.get_recent_vaults() == [str(new_vault)]

    def test_open_existing_vault_redirects_home(self, client, tmp_path):
        existing = tmp_path / "existing-vault"
        existing.mkdir()

        response = client.post("/vaults/open", data={"new_vault_path": str(existing)})

        assert response.status_code == 303
        assert response.headers["location"].startswith("/?")
        assert query._state.vault_path == str(existing)

    def test_open_rejects_file_path(self, client, tmp_path):
        file_path = tmp_path / "not-a-vault.txt"
        file_path.write_text("hello")

        response = client.post("/vaults/open", data={"new_vault_path": str(file_path)})

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert query._state.vault_path == ""

    def test_open_rejects_legacy_layout(self, client, tmp_path):
        legacy = tmp_path / "legacy-vault"
        (legacy / "vector_vault").mkdir(parents=True)

        response = client.post("/vaults/open", data={"new_vault_path": str(legacy)})

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert query._state.vault_path == ""

    def test_open_rejects_blank_path(self, client):
        response = client.post("/vaults/open", data={"new_vault_path": "   "})

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    def test_empty_form_submissions_never_return_validation_json(self, client):
        """Empty form posts must land on a friendly page, not a 422 JSON body.

        Some browsers/FastAPI versions treat an empty form value as a missing
        required field, which used to surface a raw validation error.
        """
        cases = [
            ("/vaults/open", "new_vault_path"),
            ("/search", "query"),
            ("/keyword-search", "query"),
            ("/chat", "message"),
            ("/documents/index", "source_path"),
        ]
        for url, field in cases:
            for data in ({}, {field: ""}):
                response = client.post(url, data=data)
                assert response.status_code == 303 or (
                    response.status_code == 200
                    and "text/html" in response.headers["content-type"]
                ), f"{url} with {data!r} returned {response.status_code}"

    def test_opened_vault_confirmation_shows_on_home_page(self, client, tmp_path):
        existing = tmp_path / "confirm-vault"
        existing.mkdir()

        follow = TestClient(query.app)
        response = follow.post("/vaults/open", data={"new_vault_path": str(existing)})

        assert response.status_code == 200
        assert f"Opened vault at {existing}" in response.text


class TestDirectoryPicker:
    """Tests for the folder-picker dialog and its listing endpoint."""

    def test_api_directories_lists_visible_subdirectories(self, client, tmp_path):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "file.txt").write_text("not a directory")

        response = client.get(f"/api/directories?path={tmp_path}")

        assert response.status_code == 200
        data = response.json()
        assert data["path"] == str(tmp_path.resolve())
        assert data["parent"] == str(tmp_path.resolve().parent)
        assert data["directories"] == ["alpha", "beta"]

    def test_api_directories_defaults_to_home(self, client):
        from pathlib import Path

        response = client.get("/api/directories")

        assert response.status_code == 200
        assert response.json()["path"] == str(Path.home().resolve())

    def test_api_directories_rejects_non_directories(self, client, tmp_path):
        file_path = tmp_path / "file.txt"
        file_path.write_text("hello")

        for bad in (str(file_path), str(tmp_path / "missing")):
            response = client.get(f"/api/directories?path={bad}")
            assert response.status_code == 400, bad
            assert "error" in response.json(), bad

    def test_vaults_page_wires_picker_and_disabled_button(self, client):
        page = client.get("/vaults").text

        assert 'id="vault-browse-btn"' in page
        assert 'id="dir-picker-overlay"' in page
        assert "attachDirPicker('vault-browse-btn', 'vault-path-input'" in page
        assert "requireValue('vault-path-input', 'open-vault-btn')" in page

    def test_documents_page_wires_picker_and_disabled_button(self, client, tmp_path):
        client.post("/vaults/open", data={"new_vault_path": str(tmp_path / "v")})

        page = client.get("/documents").text

        assert 'id="source-browse-btn"' in page
        assert 'id="dir-picker-overlay"' in page
        assert "attachDirPicker('source-browse-btn', 'source-path-input'" in page
        assert "requireValue('source-path-input', 'index-btn')" in page


class TestModelSettings:
    """Tests for configuring models from the interface."""

    def test_settings_page_shows_sources_and_models(self, client):
        response = client.get("/settings")

        assert response.status_code == 200
        assert 'name="embedding_source"' in response.text
        assert 'name="embedding_model"' in response.text
        assert 'name="chat_source"' in response.text
        assert 'name="chat_model"' in response.text
        # Providers registered with talkpipe appear as options.
        assert 'value="ollama"' in response.text
        assert 'value="openai"' in response.text

    def test_saving_settings_persists_and_applies_overrides(self, client):
        response = client.post(
            "/settings",
            data={
                "embedding_source": "openai",
                "embedding_model": "text-embedding-3-large",
                "chat_source": "openai",
                "chat_model": "gpt-4o",
            },
        )

        assert response.status_code == 303
        assert query._state.embedding_source == "openai"
        assert query._state.embedding_model == "text-embedding-3-large"
        assert query._state.chat_source == "openai"
        assert query._state.chat_model == "gpt-4o"
        overrides = user_settings.get_model_overrides()
        assert overrides["chat_model"] == "gpt-4o"

        page = client.get("/settings")
        assert 'value="text-embedding-3-large"' in page.text
        assert 'value="gpt-4o"' in page.text

    def test_changing_embedding_model_warns_about_reindexing(self, client):
        response = client.post(
            "/settings",
            data={
                "embedding_source": "openai",
                "embedding_model": "text-embedding-3-large",
                "chat_source": "",
                "chat_model": "",
            },
        )

        assert response.status_code == 303
        assert "re-index" in response.headers["location"]

    def test_keeping_embedding_model_does_not_warn(self, client):
        from talkpipe_vault.pipelines.config import (
            get_embedding_model,
            get_embedding_source,
        )

        response = client.post(
            "/settings",
            data={
                "embedding_source": get_embedding_source(),
                "embedding_model": get_embedding_model(),
                "chat_source": "openai",
                "chat_model": "gpt-4o",
            },
        )

        assert response.status_code == 303
        assert "re-index" not in response.headers["location"]


class TestDocumentIndexing:
    """Tests for adding documents to a vault from the interface."""

    def test_index_requires_source_path(self, client, tmp_path):
        client.post("/vaults/open", data={"new_vault_path": str(tmp_path / "v")})

        response = client.post("/documents/index", data={"source_path": "  "})

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    def test_index_rejects_pattern_matching_no_files(self, client, tmp_path):
        client.post("/vaults/open", data={"new_vault_path": str(tmp_path / "v")})

        response = client.post(
            "/documents/index",
            data={"source_path": str(tmp_path / "nothing-here")},
        )

        assert response.status_code == 303
        assert "matched+no+files" in response.headers["location"]

    def test_resolve_source_pattern_expands_directories(self, tmp_path):
        subdir = tmp_path / "docs"
        subdir.mkdir()

        pattern = query._resolve_source_pattern(str(subdir))

        assert pattern.endswith("**/*") or pattern.endswith("**\\*")

    def test_resolve_source_pattern_keeps_globs(self):
        assert query._resolve_source_pattern("/x/**/*.md") == "/x/**/*.md"

    def _wait_for_index_job(self, client, timeout_seconds=120):
        """Poll the status endpoint until the background job finishes."""
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = client.get("/api/index-status").json()
            if not status["running"]:
                return status
            time.sleep(0.1)
        raise AssertionError("indexing job did not finish in time")

    def test_index_then_search_end_to_end(self, client, tmp_path):
        """Index and search with the default in-process model2vec embeddings."""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "note.txt").write_text(
            "Kubernetes is an open-source system for automating deployment, "
            "scaling, and management of containerized applications."
        )
        vault = tmp_path / "ui-vault"
        client.post("/vaults/open", data={"new_vault_path": str(vault)})

        response = client.post(
            "/documents/index",
            data={"source_path": str(docs), "overwrite": "true"},
        )

        assert response.status_code == 303
        assert "Indexing+started" in response.headers["location"]

        status = self._wait_for_index_job(client)
        assert status["error"] is None
        assert "Indexed" in status["message"]
        assert status["total_files"] == 1
        assert status["chunks"] >= 1

        search = client.post("/search", data={"query": "container orchestration"})
        assert search.status_code == 200
        assert "note.txt" in search.text

    def test_index_status_idle_by_default(self, client):
        status = client.get("/api/index-status").json()

        assert status["running"] is False

    def test_index_job_reports_progress_and_rejects_concurrent_runs(
        self, client, tmp_path, monkeypatch
    ):
        import threading

        release = threading.Event()
        started = threading.Event()

        def fake_index(vault_path, source_pattern, **kwargs):
            progress = kwargs["progress"]
            progress(3, 1, "/somewhere/a.txt")
            started.set()
            release.wait(timeout=30)
            return 3

        monkeypatch.setattr(query, "index_documents_into_vault", fake_index)
        monkeypatch.setattr(query, "_refresh_pipelines", lambda force=False: None)
        monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.txt").write_text("hello")
        client.post("/vaults/open", data={"new_vault_path": str(tmp_path / "v")})

        first = client.post("/documents/index", data={"source_path": str(docs)})
        assert first.status_code == 303
        assert started.wait(timeout=10)

        running = client.get("/api/index-status").json()
        assert running["running"] is True
        assert running["chunks"] == 3
        assert running["files_done"] == 1
        assert running["current_file"] == "a.txt"
        assert running["total_files"] == 1

        second = client.post("/documents/index", data={"source_path": str(docs)})
        assert second.status_code == 303
        assert "already+in+progress" in second.headers["location"]

        release.set()
        status = self._wait_for_index_job(client)
        assert status["error"] is None
        assert "Indexed 3 chunk(s)" in status["message"]
