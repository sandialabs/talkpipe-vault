"""VaultService: the TUI's in-process facade over the web app's helpers.

These tests run the real pipelines where they are cheap (model2vec embeddings
are in-process) and stub the LLM for Ask, mirroring the web-app tests.
"""

import json
import os
import time
from pathlib import Path

import pytest

from talkpipe_vault.apps import query, user_settings
from talkpipe_vault.tui.service import VaultService
from tests.conftest import build_docs_vault

SAMPLE_DOCS = Path(__file__).resolve().parents[1] / "sampledocs"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv("TALKPIPE_VAULT_ROOT", raising=False)
    monkeypatch.delenv("TALKPIPE_DOCUMENT_ROOTS", raising=False)
    yield
    # The service works on the module-level singleton; leave it empty.
    VaultService()._clear_vault()


@pytest.fixture(scope="module")
def sample_vault(tmp_path_factory):
    vault = tmp_path_factory.mktemp("vault")
    build_docs_vault(SAMPLE_DOCS / "*", vault)
    return str(vault)


def _wait(status, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = status()
        if not snap["running"]:
            return snap
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_startup_without_vault_reports_no_vault():
    service = VaultService()
    outcome = service.startup("")
    assert outcome["ok"]
    assert not outcome.get("vault_path")
    assert service.status()["vault_path"] == ""


def test_startup_opens_vault_and_remembers_it(sample_vault):
    service = VaultService()
    outcome = service.startup(sample_vault)
    assert outcome["ok"], outcome
    status = service.status()
    assert status["vault_path"] == sample_vault
    assert status["chunks"] == 3
    assert status["keyword_search_enabled"] is False
    assert user_settings.get_recent_vaults()[0] == sample_vault
    assert service.recent_vaults()[0]["open"] is True


def test_startup_resume_uses_most_recent_vault(sample_vault):
    user_settings.remember_vault(sample_vault)
    service = VaultService()
    outcome = service.startup("", resume=True)
    assert outcome["ok"]
    assert "most recently used" in outcome["message"]
    assert service.vault_path == sample_vault


def test_startup_rejects_unsupported_layout(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "vector_vault").mkdir(parents=True)
    service = VaultService()
    outcome = service.startup(str(legacy))
    assert not outcome["ok"]
    assert "vector_vault" in outcome["error"] or "legacy" in outcome["error"].lower()


def test_semantic_search_and_chunk_text(sample_vault):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]
    outcome = service.semantic_search("what is the sample document about")
    assert outcome["ok"], outcome
    assert len(outcome["results"]) == 3
    first = outcome["results"][0]
    assert {"filename", "snippet", "lookup_path", "score", "path"} <= set(first)
    chunk = service.chunk_text(first["lookup_path"], first["snippet"])
    assert chunk["ok"], chunk
    assert "Heading" in chunk["content"]
    source = service.source_file(first["lookup_path"])
    assert source["ok"], source
    assert Path(source["path"]).name.startswith("SampleDocument")
    assert service.semantic_search("   ")["results"] == []


def test_search_without_vault_fails_cleanly():
    service = VaultService()
    service.startup("")
    assert not service.semantic_search("x")["ok"]
    assert not service.keyword_search("x")["ok"]
    assert not service.ask("x")["ok"]
    assert not service.refresh()["ok"]
    assert not service.start_fulltext_index()["ok"]


def test_fulltext_index_then_keyword_search(sample_vault):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]
    no_index = service.keyword_search("heading")  # no index yet
    assert not no_index["ok"]
    # The message points at the button on the same tab, not the web page.
    assert "button above" in no_index["error"]
    started = service.start_fulltext_index()
    assert started["ok"], started
    snap = _wait(service.fulltext_status)
    assert snap["error"] is None
    assert snap["total_docs"] == 3
    assert service.status()["keyword_search_enabled"] is True
    outcome = service.keyword_search("heading")
    assert outcome["ok"], outcome
    assert outcome["results"]
    assert outcome["results"][0]["score"]


def test_ask_uses_chat_pipeline_and_citations(sample_vault, monkeypatch):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]
    monkeypatch.setattr(
        service.state,
        "chat_pipeline",
        lambda q: f"Answer to {q}\n\nSources:\n- /x/y.txt",
    )
    monkeypatch.setattr(service.state, "last_refresh_time", float("inf"))
    outcome = service.ask("What is it?")
    assert outcome["ok"], outcome
    assert outcome["answer"].startswith("Answer to What is it?")
    assert "- y.txt" in outcome["answer"]  # paths stripped without --show-source-paths
    assert outcome["citations"]
    assert "path" not in outcome["citations"][0]
    assert "Answered by" not in outcome["answered_by"]
    assert "/" in outcome["answered_by"]


def test_ask_error_adds_connection_tip(sample_vault, monkeypatch):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]

    def boom(_q):
        raise RuntimeError("ollama: connection refused")

    monkeypatch.setattr(service.state, "chat_pipeline", boom)
    monkeypatch.setattr(service.state, "last_refresh_time", float("inf"))
    outcome = service.ask("hi")
    assert not outcome["ok"]
    # The in-app fix comes first; the library's environment-variable advice after.
    assert outcome["error"].startswith("Set the Ollama server URL on the Settings tab")
    assert "connection refused" in outcome["error"]


def test_open_vault_creates_and_confirms_non_vault_folders(tmp_path):
    service = VaultService()
    service.startup("")
    new = tmp_path / "fresh-vault"
    outcome = service.open_vault(str(new))
    assert outcome["ok"], outcome
    assert outcome["created"] is True
    assert service.vault_path == str(new)

    messy = tmp_path / "messy"
    messy.mkdir()
    (messy / "notes.txt").write_text("hello")
    outcome = service.open_vault(str(messy))
    assert not outcome["ok"]
    assert outcome["needs_confirm"] is True
    assert outcome["entry_count"] == 1
    outcome = service.open_vault(str(messy), confirm_non_vault=True)
    assert outcome["ok"], outcome
    assert "already holds files" in outcome["message"]
    assert not service.open_vault("")["ok"]


def test_delete_vault_rules(tmp_path):
    service = VaultService()
    service.startup("")
    victim = tmp_path / "victim"
    assert service.open_vault(str(victim))["ok"]
    other = tmp_path / "other"
    assert service.open_vault(str(other))["ok"]  # now victim is not the open one
    assert not service.delete_vault(str(tmp_path / "unknown"))["ok"]
    assert not service.delete_vault(str(other))["ok"]  # currently open
    outcome = service.delete_vault(str(victim))
    assert outcome["ok"], outcome
    assert not victim.exists()
    assert str(victim) not in user_settings.get_recent_vaults()


def test_list_directories_matches_web_api(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / ".hidden").mkdir()
    service = VaultService()
    listing = service.list_directories(str(tmp_path))
    assert listing["ok"]
    assert listing["directories"] == ["a", "b"]
    assert listing["path"] == str(tmp_path)
    bad = service.list_directories(str(tmp_path / "nope"))
    assert not bad["ok"]
    assert "error" in bad


def test_start_indexing_validates_then_indexes(tmp_path):
    service = VaultService()
    service.startup("")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "one.txt").write_text("The quick brown fox jumps over the lazy dog. " * 5)
    vault = tmp_path / "vault"
    assert not service.start_indexing("", "")["ok"]
    assert not service.start_indexing(str(tmp_path / "missing"), str(vault))["ok"]
    assert not service.start_indexing(str(docs), str(docs))["ok"]  # vault == source
    outcome = service.start_indexing(str(docs), str(vault))
    assert outcome["ok"], outcome
    assert "Created vault" in outcome["message"]
    snap = _wait(service.index_status, timeout=180)
    assert snap["error"] is None, snap
    assert snap["chunks"] >= 1
    service.refresh()
    assert service.status()["chunks"] >= 1
    assert service.vault_embedding_record()["source"]
    # A second run into the open vault needs no vault argument.
    outcome = service.start_indexing(str(docs), overwrite=True)
    assert outcome["ok"], outcome
    _wait(service.index_status, timeout=180)


def test_settings_round_trip(sample_vault):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]
    view = service.settings_view()
    assert "model2vec" in view["embedding_sources"]
    assert view["models"]["embedding_model"]
    assert {c["key"] for c in view["credentials"]} >= {
        "openai_api_key",
        "ollama_server_url",
    }

    assert not service.save_settings(chunk_size="abc")["ok"]
    assert not service.save_settings(shingle_size="3", shingle_overlap="5")["ok"]
    outcome = service.save_settings(
        chat_source="ollama", chat_model="llama3.2", rag_result_limit="7"
    )
    assert outcome["ok"], outcome
    view = service.settings_view()
    assert view["overrides"]["chat_model"] == "llama3.2"
    assert view["overrides"]["rag_result_limit"] == 7
    assert (
        json.loads(Path(view["settings_file"]).read_text())["chat_model"] == "llama3.2"
    )

    outcome = service.save_credentials({"ollama_server_url": "http://example:11434"})
    assert outcome["ok"], outcome
    creds = {c["key"]: c for c in service.settings_view()["credentials"]}
    assert creds["ollama_server_url"]["value"] == "http://example:11434"


def test_config_status_without_probe(sample_vault):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]
    report = service.config_status(probe=False)
    names = [c["name"] for c in report["checks"]]
    assert "Embeddings provider" in names[0] or "Embeddings" in names[0]
    assert report["overall"]


def test_retrieval_filter_lifecycle(sample_vault):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]
    assert service.filter_view()["available"] is True
    assert not service.save_filter(
        action="save", script="", enabled=True, strict=False
    )["ok"]
    bad = service.save_filter(
        action="validate", script="INPUT FROM (", enabled=True, strict=False
    )
    assert not bad["ok"]
    ok_script = '| lambdaFilter[expression="score > 0.5"]'
    good = service.save_filter(
        action="validate", script=ok_script, enabled=True, strict=False
    )
    assert good["ok"], good
    # Saving without enabling says how to turn it on rather than looking done.
    saved_off = service.save_filter(
        action="save", script=ok_script, enabled=False, strict=False
    )
    assert saved_off["ok"], saved_off
    assert "not enabled" in saved_off["message"]
    assert "Enabled on this machine" in saved_off["message"]
    assert service.filter_view()["enabled"] is False
    saved = service.save_filter(
        action="save", script=ok_script, enabled=True, strict=False
    )
    assert saved["ok"], saved
    assert service.filter_view()["enabled"] is True
    assert service.status()["filter_active"] is True
    removed = service.save_filter(
        action="remove", script="", enabled=False, strict=False
    )
    assert removed["ok"]
    assert service.filter_view()["script"] == ""


def test_service_never_touches_the_web_app_module_state_shape():
    """The facade only reads/writes AppState fields that exist on the web app."""
    service = VaultService()
    for attr in (
        "vault_path",
        "search_pipeline",
        "chat_pipeline",
        "keyword_search_enabled",
    ):
        assert hasattr(query._state, attr)
    assert service.state is query._state


def test_startup_resume_names_the_missing_vault_it_skipped(sample_vault, tmp_path):
    gone = tmp_path / "gone-vault"
    user_settings.remember_vault(sample_vault)
    user_settings.remember_vault(str(gone))  # most recent, but not on disk
    service = VaultService()
    outcome = service.startup("", resume=True)
    assert outcome["ok"]
    assert service.vault_path == sample_vault
    assert str(gone) in outcome["message"]
    assert "no longer exists" in outcome["message"]


def test_status_reports_full_text_index_older_than_the_vault(sample_vault):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]
    assert service.status()["keyword_index_stale"] is False  # no index at all
    assert service.start_fulltext_index()["ok"]
    assert _wait(service.fulltext_status)["error"] is None
    assert service.status()["keyword_index_stale"] is False
    # Documents indexed after the full-text index was built (e.g. by
    # vault-server) make it stale until it is rebuilt.
    newest = time.time() + 1
    for entry in Path(sample_vault, "docs.lance").rglob("*"):
        if entry.is_file():
            os.utime(entry, (newest, newest))
    assert service.status()["keyword_index_stale"] is True
    outcome = service.keyword_search("nothing-matches-this")
    assert outcome["ok"]
    assert "out of date" in outcome["note"]
    assert "Rebuild full-text index" in outcome["empty_text"]
    time.sleep(1.1)  # so the rebuilt index is newer than the touched table
    assert service.start_fulltext_index()["ok"]
    assert _wait(service.fulltext_status)["error"] is None
    assert service.status()["keyword_index_stale"] is False


def test_startup_asks_before_making_a_documents_folder_a_vault(tmp_path):
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "a.md").write_text("# a\n")
    (docs / "b.md").write_text("# b\n")
    service = VaultService()
    outcome = service.startup(str(docs))
    assert not outcome["ok"]
    assert outcome["needs_confirm"]
    assert outcome["confirm_path"] == str(docs)
    assert outcome["entry_count"] == 2
    assert service.vault_path == ""
    # Confirmed, the same call opens it (the Vault form's flow).
    confirmed = service.startup(str(docs), confirm_non_vault=True)
    assert confirmed["ok"], confirmed
    assert service.vault_path == str(docs)


def test_startup_reports_what_it_is_waiting_on(tmp_path):
    seen: list[str] = []
    service = VaultService()
    outcome = service.startup(str(tmp_path / "new-vault"), progress=seen.append)
    assert outcome["ok"], outcome
    assert len(seen) == 1
    assert "loading the embedding model model2vec/" in seen[0]
    assert str(tmp_path / "new-vault") in seen[0]


def test_opening_note_mentions_the_download_when_the_model_is_not_cached(
    monkeypatch,
):
    from talkpipe_vault.pipelines import diagnostics

    service = VaultService()
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda _model: ("absent", None)
    )
    note = service.opening_note("/tmp/v")
    assert "downloaded from Hugging Face" in note
    assert "250 MB" in note
    monkeypatch.setattr(
        diagnostics, "_model2vec_cache_state", lambda _model: ("ready", "/cache")
    )
    assert "downloaded" not in service.opening_note("/tmp/v")


def test_startup_explains_an_unusable_parent(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    outcome = VaultService().startup(str(blocker / "vault"))
    assert not outcome["ok"]
    assert f"the parent {blocker} is a file, not a folder" in outcome["error"]

    if os.geteuid() == 0:  # pragma: no cover - root can write anywhere
        pytest.skip("permissions do not apply to root")
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    sealed.chmod(0o555)
    try:
        outcome = VaultService().startup(str(sealed / "missing" / "vault"))
    finally:
        sealed.chmod(0o755)
    assert not outcome["ok"]
    assert f"the parent folder {sealed / 'missing'} does not exist" in outcome["error"]
    assert "Permission denied" in outcome["error"]


def test_ask_timeout_names_the_ollama_server(sample_vault, monkeypatch):
    service = VaultService()
    assert service.startup(sample_vault)["ok"]

    def slow(_q):
        raise TimeoutError("timed out")

    monkeypatch.setattr(service.state, "chat_pipeline", slow)
    monkeypatch.setattr(service.state, "last_refresh_time", float("inf"))
    monkeypatch.setenv("TALKPIPE_OLLAMA_SERVER_URL", "http://10.255.255.1:11434")
    monkeypatch.setattr(
        query,
        "_effective_models",
        lambda _state: {"chat_source": "ollama", "chat_model": "m"},
    )
    outcome = service.ask("hello")
    assert not outcome["ok"]
    assert "http://10.255.255.1:11434" in outcome["error"]
    assert "Settings tab (F6)" in outcome["error"]
    assert "timed out" in outcome["error"]


def test_source_file_reports_whether_it_is_text(tmp_path):
    from talkpipe_vault.tui.service import looks_like_text

    csv = tmp_path / "rows.csv"
    csv.write_text("a,b\n1,2\n")
    binary = tmp_path / "blob.bin"
    binary.write_bytes(bytes(range(256)))
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    assert looks_like_text(csv)
    assert not looks_like_text(binary)
    assert looks_like_text(empty)
    assert not looks_like_text(tmp_path / "missing.txt")
