"""Tests for the optional path fences (TALKPIPE_VAULT_ROOT / TALKPIPE_DOCUMENT_ROOTS)."""

import os
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talkpipe_vault.apps import access_control, query, user_settings


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Isolate user settings and start every test unrestricted."""
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "vault-home"))
    monkeypatch.delenv(access_control.VAULT_ROOT_ENV, raising=False)
    monkeypatch.delenv(access_control.DOCUMENT_ROOTS_ENV, raising=False)
    query._state.vault_path = ""
    query._state.search_pipeline = None
    query._state.chat_pipeline = None
    query._state.keyword_search_pipeline = None


@pytest.fixture
def client():
    return TestClient(query.app)


def _error_from_redirect(response) -> str:
    assert response.status_code == 303
    params = urllib.parse.parse_qs(
        urllib.parse.urlparse(response.headers["location"]).query
    )
    return params.get("error", [""])[0]


def _message_from_redirect(response) -> str:
    assert response.status_code == 303
    params = urllib.parse.parse_qs(
        urllib.parse.urlparse(response.headers["location"]).query
    )
    return params.get("message", [""])[0]


class TestHelpers:
    def test_unset_means_unrestricted(self):
        assert access_control.vault_root() is None
        assert access_control.document_roots() == []
        assert access_control.browse_roots() == []
        assert access_control.is_allowed("/anywhere/at/all", [])

    def test_empty_and_whitespace_mean_unset(self, monkeypatch):
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, "  ")
        monkeypatch.setenv(access_control.DOCUMENT_ROOTS_ENV, f" {os.pathsep} ")
        assert access_control.vault_root() is None
        assert access_control.document_roots() == []

    def test_multiple_document_roots(self, tmp_path, monkeypatch):
        first = tmp_path / "a"
        second = tmp_path / "b"
        monkeypatch.setenv(
            access_control.DOCUMENT_ROOTS_ENV,
            f"{first}{os.pathsep}{second}{os.pathsep}{first}",
        )
        assert access_control.document_roots() == [first, second]

    def test_containment(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        (root / "inner").mkdir(parents=True)
        assert access_control.is_allowed(root / "inner", [root])
        assert access_control.is_allowed(root, [root])
        assert not access_control.is_allowed(tmp_path, [root])
        # Dot-dot segments are resolved before the check.
        assert not access_control.is_allowed(root / ".." / "outside", [root])

    def test_symlink_escape_is_caught(self, tmp_path):
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        link = root / "sneaky"
        link.symlink_to(outside)
        assert not access_control.is_allowed(link, [root])

    def test_confine_returns_resolved_path_inside_roots(self, tmp_path):
        root = tmp_path / "root"
        (root / "inner").mkdir(parents=True)
        confined = access_control.confine(root / "x" / ".." / "inner", [root])
        assert confined == (root / "inner").resolve()
        assert access_control.confine(tmp_path / "outside", [root]) is None

    def test_confine_unrestricted_still_resolves(self, tmp_path):
        confined = access_control.confine(tmp_path / "a" / ".." / "b", [])
        assert confined == (tmp_path / "b").resolve()

    def test_confine_rejects_sibling_prefix_name(self, tmp_path):
        # "/root-evil" must not pass a check against "/root": containment is
        # a path-component check, not a raw string prefix.
        root = tmp_path / "root"
        root.mkdir()
        evil = tmp_path / "root-evil"
        evil.mkdir()
        assert access_control.confine(evil, [root]) is None

    def test_startup_errors_for_missing_roots(self, tmp_path, monkeypatch):
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(tmp_path / "nope"))
        monkeypatch.setenv(
            access_control.DOCUMENT_ROOTS_ENV, str(tmp_path / "also-nope")
        )
        errors = access_control.startup_errors()
        assert len(errors) == 2
        assert "nope" in errors[0]

    def test_startup_errors_empty_when_unrestricted(self):
        assert access_control.startup_errors() == []


class TestDirectoriesApi:
    def test_unrestricted_defaults_to_home(self, client):
        data = client.get("/api/directories").json()
        assert data["path"] == str(Path.home())
        assert data["home"] == str(Path.home())

    def test_restricted_defaults_to_root(self, client, tmp_path, monkeypatch):
        root = tmp_path / "docs"
        (root / "sub").mkdir(parents=True)
        monkeypatch.setenv(access_control.DOCUMENT_ROOTS_ENV, str(root))
        data = client.get("/api/directories").json()
        assert data["path"] == str(root)
        assert data["parent"] is None  # cannot browse above the only root
        assert data["home"] == str(root)
        assert data["directories"] == ["sub"]

    def test_restricted_rejects_outside_paths(self, client, tmp_path, monkeypatch):
        root = tmp_path / "docs"
        root.mkdir()
        monkeypatch.setenv(access_control.DOCUMENT_ROOTS_ENV, str(root))
        response = client.get("/api/directories", params={"path": str(tmp_path)})
        assert response.status_code == 400
        assert str(root) in response.json()["error"]

    def test_multiple_roots_virtual_top_level(self, client, tmp_path, monkeypatch):
        vault_root = tmp_path / "data"
        doc_root = tmp_path / "docs"
        vault_root.mkdir()
        doc_root.mkdir()
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(vault_root))
        monkeypatch.setenv(access_control.DOCUMENT_ROOTS_ENV, str(doc_root))

        top = client.get("/api/directories").json()
        assert top["path"] == ""
        assert top["parent"] is None
        assert top["directories"] == [str(vault_root), str(doc_root)]

        # From a root, "up" leads back to the virtual top level.
        at_root = client.get("/api/directories", params={"path": str(doc_root)}).json()
        assert at_root["parent"] == ""


class TestVaultRoutes:
    def test_open_outside_root_is_refused(self, client, tmp_path, monkeypatch):
        root = tmp_path / "data"
        root.mkdir()
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(root))
        outside = tmp_path / "elsewhere" / "vault"
        response = client.post(
            "/vaults/open",
            data={"new_vault_path": str(outside)},
            follow_redirects=False,
        )
        assert str(root) in _error_from_redirect(response)
        assert not outside.exists()

    def test_open_inside_root_is_allowed(self, client, tmp_path, monkeypatch):
        root = tmp_path / "data"
        root.mkdir()
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(root))
        inside = root / "new-vault"
        response = client.post(
            "/vaults/open",
            data={"new_vault_path": str(inside)},
            follow_redirects=False,
        )
        assert _error_from_redirect(response) == ""
        assert inside.is_dir()

    def test_relative_name_is_placed_under_root(self, client, tmp_path, monkeypatch):
        root = tmp_path / "data"
        root.mkdir()
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(root))
        response = client.post(
            "/vaults/open",
            data={"new_vault_path": "my-vault"},
            follow_redirects=False,
        )
        message = _message_from_redirect(response)
        assert f"Created new vault at {root / 'my-vault'}" in message
        # The user is told the name was placed under the root automatically.
        assert "placed there automatically" in message
        assert (root / "my-vault").is_dir()

    def test_relative_name_cannot_escape_root(self, client, tmp_path, monkeypatch):
        root = tmp_path / "data"
        root.mkdir()
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(root))
        response = client.post(
            "/vaults/open",
            data={"new_vault_path": "../escape"},
            follow_redirects=False,
        )
        assert str(root) in _error_from_redirect(response)
        assert not (tmp_path / "escape").exists()

    def test_delete_outside_root_only_forgets(self, client, tmp_path, monkeypatch):
        root = tmp_path / "data"
        root.mkdir()
        outside = tmp_path / "old-vault"
        outside.mkdir()
        (outside / "keep-me.txt").write_text("data")
        user_settings.remember_vault(str(outside))
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(root))

        response = client.post(
            "/vaults/delete",
            data={"vault_path": str(outside), "confirm": "delete"},
            follow_redirects=False,
        )
        assert "left untouched" in _message_from_redirect(response)
        assert (outside / "keep-me.txt").exists()
        assert str(outside) not in user_settings.get_recent_vaults()


class TestIndexRoute:
    def test_index_outside_document_roots_is_refused(
        self, client, tmp_path, monkeypatch
    ):
        doc_root = tmp_path / "docs"
        doc_root.mkdir()
        outside = tmp_path / "secrets"
        outside.mkdir()
        (outside / "file.txt").write_text("text")
        monkeypatch.setenv(access_control.DOCUMENT_ROOTS_ENV, str(doc_root))
        query._state.vault_path = str(tmp_path / "vault")

        response = client.post(
            "/documents/index",
            data={"source_path": str(outside)},
            follow_redirects=False,
        )
        assert str(doc_root) in _error_from_redirect(response)


class TestNonGlobPrefix:
    def test_plain_directory(self):
        assert query._nonglob_prefix("/data/docs/**/*") == "/data/docs"

    def test_wildcard_midway(self):
        assert query._nonglob_prefix("/data/*/notes/*.md") == "/data"

    def test_relative_pattern_with_leading_wildcard(self):
        assert query._nonglob_prefix("**/*.md") == "."


class TestVaultPathHint:
    def test_suggests_home_path_when_unrestricted(self, client):
        response = client.get("/vaults")
        assert "~/my-vault" in response.text

    def test_suggests_path_under_vault_root_when_fenced(
        self, tmp_path, monkeypatch, client
    ):
        root = tmp_path / "data"
        root.mkdir()
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(root))
        response = client.get("/vaults")
        assert str(root.resolve() / "my-vault") in response.text
        assert "~/my-vault" not in response.text


class TestRunAppStartup:
    def test_missing_root_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(tmp_path / "missing"))
        with pytest.raises(ValueError, match="not a directory"):
            query.run_app(open_browser=False)

    def test_vault_outside_root_fails_loudly(self, tmp_path, monkeypatch):
        root = tmp_path / "data"
        root.mkdir()
        monkeypatch.setenv(access_control.VAULT_ROOT_ENV, str(root))
        with pytest.raises(ValueError, match="outside"):
            query.run_app(vault_path=str(tmp_path / "elsewhere"), open_browser=False)

    def test_pipeline_failure_still_starts_server(self, tmp_path, monkeypatch, capsys):
        """A vault whose embedder cannot load (e.g. model not cached and
        Hugging Face unreachable) must degrade to a no-vault start, not keep
        the server from coming up."""

        def broken_init(path):
            raise RuntimeError("model download failed")

        served = {}
        monkeypatch.setattr(query, "init_pipelines", broken_init)
        monkeypatch.setattr(
            query.uvicorn, "run", lambda *a, **k: served.update(started=True)
        )
        query.run_app(vault_path=str(tmp_path / "vault"), open_browser=False)
        assert served.get("started")
        assert query._state.vault_path == ""
        assert "model download failed" in capsys.readouterr().err
