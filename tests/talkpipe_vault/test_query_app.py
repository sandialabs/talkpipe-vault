"""Unit tests for the vault query web app."""

import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from talkpipe_vault.apps import query
from talkpipe_vault.pipelines import searching_and_prompting
from talkpipe_vault.pipelines.searching_and_prompting import VaultTextSearch


def _wait_for_fulltext_job(timeout: float = 10.0) -> dict:
    """Block until the background full-text index build finishes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = query._fulltext_index_job_snapshot()
        if not snapshot["running"] and (snapshot["message"] or snapshot["error"]):
            return snapshot
        time.sleep(0.02)
    raise AssertionError("full-text index build did not finish in time")


class _FakeLanceTable:
    def __init__(self, rows):
        self.rows = rows

    def to_arrow(self):
        return self

    def to_pylist(self):
        return self.rows


class _FakeDocStore:
    def __init__(self, rows):
        self.rows = rows

    def _get_table(self):
        return _FakeLanceTable(self.rows), None

    def count(self):
        return len(self.rows)


@pytest.fixture(autouse=True)
def reset_app_state(tmp_path, monkeypatch):
    """Reset global query app state between tests."""
    from talkpipe_vault.apps import user_settings

    monkeypatch.setenv(user_settings.VAULT_HOME_ENV, str(tmp_path / "vault-home"))
    query._state.vault_path = str(tmp_path)
    query._state.search_pipeline = None
    query._state.chat_pipeline = None
    query._state.keyword_search_pipeline = None
    query._state.shingled_chunks_count = 0
    query._state.keyword_search_enabled = False
    query._state.show_source_paths = False
    query._state.last_refresh_time = 0.0
    query._state.embedding_model = None
    query._state.embedding_source = None
    query._state.chat_model = None
    query._state.chat_source = None
    query._state.chunk_size = None
    query._state.shingle_size = None
    query._state.shingle_overlap = None
    query._state.rag_result_limit = None
    yield


def test_keyword_search_page_is_disabled_without_whoosh():
    """Keyword search UI should be disabled when no Whoosh index is available."""
    client = TestClient(query.app)

    response = client.get("/keyword-search")

    assert response.status_code == 200
    assert 'id="search-input"' in response.text
    assert 'id="search-btn" disabled' in response.text
    assert "Keyword search is disabled." in response.text
    assert "Create Full-Text Index" in response.text


def test_keyword_search_page_can_rebuild_existing_whoosh_index():
    """Keyword search UI should expose index rebuild when Whoosh is available."""
    query._state.keyword_search_enabled = True
    client = TestClient(query.app)

    response = client.get("/keyword-search")

    assert response.status_code == 200
    assert "Rebuild Full-Text Index" in response.text
    assert 'action="/keyword-search/create-index"' in response.text


def test_config_status_endpoint_returns_report(monkeypatch):
    """The config-status endpoint should return a rolled-up report as JSON."""
    from talkpipe_vault.pipelines import diagnostics

    monkeypatch.setattr(
        diagnostics,
        "_ollama_tags",
        lambda url, timeout: (["mistral-small:latest"], None),
    )
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (True, 384),
    )
    client = TestClient(query.app)

    response = client.get("/api/config-status")

    assert response.status_code == 200
    body = response.json()
    assert body["overall"] in {"ok", "warn", "error", "unknown"}
    names = {check["name"] for check in body["checks"]}
    assert {"Embeddings provider", "Chat (Ask) provider"} <= names


def test_config_status_endpoint_skips_probe(monkeypatch):
    """?probe=0 should avoid live provider calls and report unknown for Ollama."""

    def _fail(*_args, **_kwargs):
        raise AssertionError("network probe should not run when probe=0")

    from talkpipe_vault.pipelines import diagnostics

    monkeypatch.setattr(diagnostics, "_ollama_tags", _fail)
    client = TestClient(query.app)

    response = client.get("/api/config-status?probe=0")

    assert response.status_code == 200
    chat = next(
        c for c in response.json()["checks"] if c["name"] == "Chat (Ask) provider"
    )
    assert chat["status"] == "unknown"


def test_config_status_endpoint_passes_download_flag(monkeypatch):
    """?download=1 (Re-test) must reach diagnostics as allow_download=True."""
    from talkpipe_vault.pipelines import diagnostics

    captured = {}

    def fake_collect(models, **kwargs):
        captured.update(kwargs)
        return {"overall": "ok", "checks": []}

    monkeypatch.setattr(diagnostics, "collect_config_status", fake_collect)
    client = TestClient(query.app)

    assert client.get("/api/config-status?download=1").status_code == 200
    assert captured["allow_download"] is True

    assert client.get("/api/config-status").status_code == 200
    assert captured["allow_download"] is False


def test_save_credentials_persists_and_applies(monkeypatch):
    """Posting credentials should store them and apply to the environment."""
    from talkpipe_vault.apps import credentials

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TALKPIPE_OLLAMA_SERVER_URL", raising=False)
    credentials._managed_env.clear()
    monkeypatch.setattr(query, "_refresh_pipelines", lambda force=False: None)
    client = TestClient(query.app)

    response = client.post(
        "/settings/credentials",
        data={
            "openai_api_key": "sk-ui-123456",
            "ollama_server_url": "http://ollama.example:11434",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert os.environ["OPENAI_API_KEY"] == "sk-ui-123456"
    assert os.environ["TALKPIPE_OLLAMA_SERVER_URL"] == "http://ollama.example:11434"
    assert credentials.load()["openai_api_key"] == "sk-ui-123456"
    credentials._managed_env.clear()


def test_save_credentials_blank_secret_keeps_saved_key(monkeypatch):
    """A blank secret field must not wipe an already-saved key."""
    from talkpipe_vault.apps import credentials

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    credentials._managed_env.clear()
    credentials.set_values({"openai_api_key": "sk-existing-9999"})
    monkeypatch.setattr(query, "_refresh_pipelines", lambda force=False: None)
    client = TestClient(query.app)

    client.post(
        "/settings/credentials",
        data={"openai_api_key": "", "ollama_server_url": ""},
        follow_redirects=False,
    )

    assert credentials.load()["openai_api_key"] == "sk-existing-9999"
    credentials._managed_env.clear()


def test_save_credentials_clear_checkbox_removes_key(monkeypatch):
    """The clear checkbox removes a saved secret even with a blank field."""
    from talkpipe_vault.apps import credentials

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    credentials._managed_env.clear()
    credentials.set_values({"openai_api_key": "sk-existing-9999"})
    monkeypatch.setattr(query, "_refresh_pipelines", lambda force=False: None)
    client = TestClient(query.app)

    client.post(
        "/settings/credentials",
        data={"openai_api_key": "", "clear_openai_api_key": "true"},
        follow_redirects=False,
    )

    assert "openai_api_key" not in credentials.load()
    assert "OPENAI_API_KEY" not in os.environ
    credentials._managed_env.clear()


def test_settings_page_shows_credentials_form_without_leaking_secret(monkeypatch):
    """The settings page should show the credentials form and never echo a key."""
    from talkpipe_vault.apps import credentials

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    credentials._managed_env.clear()
    credentials.set_values({"openai_api_key": "sk-topsecret-4242"})
    client = TestClient(query.app)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Connections &amp; credentials" in response.text
    assert 'action="/settings/credentials"' in response.text
    assert "sk-topsecret-4242" not in response.text  # secret never rendered
    assert "4242" in response.text  # but the masked hint is shown
    credentials._managed_env.clear()


def test_settings_page_shows_resolved_credentials_path(tmp_path):
    """The credentials panel should show the concrete file path, not a variable."""
    client = TestClient(query.app)

    response = client.get("/settings")

    assert response.status_code == 200
    expected = str(tmp_path / "vault-home" / "credentials.json")
    assert expected in response.text
    assert "$TALKPIPE_VAULT_HOME/credentials.json" not in response.text


def test_settings_page_shows_config_status_panel():
    """The settings page should host the configuration status panel."""
    client = TestClient(query.app)

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Configuration status" in response.text
    assert 'id="config-status-body"' in response.text
    assert "/api/config-status" in response.text


def test_header_shows_chunk_count_without_full_text_index(monkeypatch):
    """Header should show the docs-table chunk count and no full-text stat."""
    rows = [
        {"id": "chunk-1", "document": {"content": "one"}},
        {"id": "chunk-2", "document": {"content": "two"}},
    ]
    monkeypatch.setattr(
        query,
        "LanceDBDocumentStore",
        lambda **_: _FakeDocStore(rows),
    )
    monkeypatch.setattr(query, "_keyword_search_enabled", lambda _vault_path: True)
    client = TestClient(query.app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Chunks:" in response.text
    assert '<span class="vault-stat-value">2</span>' in response.text
    assert "Full-Text Index:" not in response.text
    assert "Shingled Chunks:" not in response.text


def test_header_uses_existing_svg_logo_and_refresh_control():
    """Header should use packaged assets and expose a manual refresh action."""
    client = TestClient(query.app)

    response = client.get("/")

    assert response.status_code == 200
    assert 'src="/static/favicon.svg"' in response.text
    assert 'action="/refresh"' in response.text
    assert 'name="return_to" value="/"' in response.text


def test_init_pipelines_rejects_legacy_nested_vector_layout(tmp_path):
    """Legacy vector_vault layout should fail fast during query init."""
    (tmp_path / "vector_vault").mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="Unsupported legacy vault layout"):
        query.init_pipelines(str(tmp_path))


def test_refresh_redirects_back_to_current_page(monkeypatch):
    """Refresh should return to the page where the user triggered it."""
    called: dict[str, object] = {}
    monkeypatch.setattr(
        query,
        "_refresh_pipelines",
        lambda force=False: called.setdefault("force", force),
    )
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post(
        "/refresh", data={"return_to": "/keyword-search"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keyword-search"
    assert called["force"] is True


def test_keyword_search_post_does_not_run_without_whoosh(monkeypatch):
    """Keyword search POST should not call the pipeline without a Whoosh index."""
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    query._state.keyword_search_pipeline = pytest.fail
    client = TestClient(query.app)

    response = client.post("/keyword-search", data={"query": "FastAPI"})

    assert response.status_code == 200
    assert "Keyword search is disabled because this vault has no Whoosh index." in (
        response.text
    )


def test_create_whoosh_index_route_builds_searchable_index(
    monkeypatch, capsys, tmp_path
):
    """Create-index endpoint should build a real, searchable Whoosh index.

    The index is built for real (not mocked) because talkpipe reserves the
    doc_id schema field; a mocked builder previously hid an incompatibility.
    """
    query._state.vault_path = str(tmp_path)
    monkeypatch.setattr(
        query,
        "_iter_lancedb_docs_for_whoosh",
        lambda _vault_path: [
            {
                "doc_id": "row-1",
                "content": "abc def",
                "path": "a.txt",
                "filename": "a.txt",
            }
        ],
    )
    monkeypatch.setattr(query, "_refresh_pipelines", lambda force=False: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)

    client = TestClient(query.app)
    response = client.post("/keyword-search/create-index", follow_redirects=False)

    # The build runs in the background; the POST just redirects to the page,
    # which then polls for progress and reloads with the outcome.
    assert response.status_code == 303
    assert response.headers["location"] == "/keyword-search"

    snapshot = _wait_for_fulltext_job()
    assert snapshot["error"] is None
    assert snapshot["message"] == "Full-text index created with 1 document(s)."

    # The index exists, keeps the stable doc_id, and is searchable.
    from talkpipe.search.whoosh import WhooshFullTextIndex

    with WhooshFullTextIndex(query.get_whoosh_index_path(str(tmp_path))) as ix:
        hits = ix.text_search("abc")
        assert [hit.doc_id for hit in hits] == ["row-1"]
        assert hits[0].document["path"] == "a.txt"

    output = capsys.readouterr().out
    assert "Building full-text index: 1 document(s)" in output
    assert "Full-text index built: 1 document(s)" in output


def test_create_whoosh_index_route_replaces_existing_index(monkeypatch, tmp_path):
    """Rebuilding the index should replace prior contents, not append."""
    query._state.vault_path = str(tmp_path)
    documents = [
        {"doc_id": "old", "content": "stale text", "path": "old.txt", "filename": ""}
    ]
    monkeypatch.setattr(
        query, "_iter_lancedb_docs_for_whoosh", lambda _vault_path: documents
    )
    monkeypatch.setattr(query, "_refresh_pipelines", lambda force=False: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)
    client.post("/keyword-search/create-index", follow_redirects=False)
    _wait_for_fulltext_job()

    documents[:] = [
        {"doc_id": "new", "content": "fresh text", "path": "new.txt", "filename": ""}
    ]
    client.post("/keyword-search/create-index", follow_redirects=False)
    _wait_for_fulltext_job()

    from talkpipe.search.whoosh import WhooshFullTextIndex

    with WhooshFullTextIndex(query.get_whoosh_index_path(str(tmp_path))) as ix:
        assert ix.text_search("stale") == []
        assert [hit.doc_id for hit in ix.text_search("fresh")] == ["new"]


def test_iter_lancedb_docs_for_whoosh_preserves_source_path(monkeypatch):
    """Whoosh index documents should use LanceDB source paths, not row ids."""
    rows = [
        {
            "id": "row-uuid",
            "document": {
                "content": "indexed text",
                "source": "/original/source.pdf",
                "id": "/original/source.pdf",
                "title": "source.pdf",
            },
        }
    ]
    monkeypatch.setattr(
        query,
        "LanceDBDocumentStore",
        lambda **_: _FakeDocStore(rows),
    )
    monkeypatch.setattr(
        query, "ensure_supported_vault_layout", lambda _vault_path: None
    )

    documents = query._iter_lancedb_docs_for_whoosh("/tmp/test-vault")

    assert documents == [
        {
            "doc_id": "row-uuid",
            "content": "indexed text",
            "path": "/original/source.pdf",
            "filename": "source.pdf",
        }
    ]


def test_keyword_search_results_hide_source_path_by_default(monkeypatch):
    """Keyword results should not show source paths unless explicitly enabled."""
    query._state.keyword_search_enabled = True
    query._state.keyword_search_pipeline = lambda _query: [
        {
            "doc_id": "row-uuid",
            "score": 1.0,
            "document": {
                "content": "indexed text",
                "path": "/original/source.pdf",
                "filename": "source.pdf",
            },
        }
    ]
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post("/keyword-search", data={"query": "indexed"})

    assert response.status_code == 200
    assert "/original/source.pdf" not in response.text
    assert 'href="/source-file?path=' not in response.text


def test_keyword_search_results_display_source_path_as_server_link(monkeypatch):
    """Keyword results should show the indexed source path as a hyperlink."""
    query._state.show_source_paths = True
    query._state.keyword_search_enabled = True
    query._state.keyword_search_pipeline = lambda _query: [
        {
            "doc_id": "row-uuid",
            "score": 1.0,
            "document": {
                "content": "indexed text",
                "path": "/original/source.pdf",
                "filename": "source.pdf",
            },
        }
    ]
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post("/keyword-search", data={"query": "indexed"})

    assert response.status_code == 200
    assert 'href="/source-file?path=' in response.text
    assert "/original/source.pdf" in response.text


def test_keyword_search_results_copy_button_targets_full_chunk(monkeypatch):
    """Keyword results should expose lookup metadata for full chunk copy."""
    query._state.keyword_search_enabled = True
    query._state.keyword_search_pipeline = lambda _query: [
        {
            "doc_id": "row-uuid",
            "score": 1.0,
            "document": {
                "content": "indexed text",
                "path": "/original/source.pdf",
                "filename": "source.pdf",
            },
        }
    ]
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post("/keyword-search", data={"query": "indexed"})

    assert response.status_code == 200
    assert 'class="copy-snippet-btn"' in response.text
    assert 'data-path="row-uuid"' in response.text
    assert 'data-snippet="indexed text"' in response.text
    assert "Copy Chunk" in response.text


def test_process_keyword_results_keeps_multiple_hits_for_same_source_path():
    """Keyword search should show every matching indexed row from a source file."""
    raw_results = [
        {
            "doc_id": "chunk-1",
            "score": 2.0,
            "document": {
                "content": "first matching chunk",
                "path": "/original/source.pdf",
                "filename": "source.pdf",
            },
        },
        {
            "doc_id": "chunk-2",
            "score": 1.5,
            "document": {
                "content": "second matching chunk",
                "path": "/original/source.pdf",
                "filename": "source.pdf",
            },
        },
    ]

    results = query._process_keyword_results(raw_results)

    assert len(results) == 2
    assert [result["snippet"] for result in results] == [
        "first matching chunk",
        "second matching chunk",
    ]
    assert {result["path"] for result in results} == {"/original/source.pdf"}


def test_process_keyword_results_uses_source_path_for_flat_whoosh_hits():
    """Flat Whoosh hits should remain clickable when doc_id is absent."""
    raw_results = [
        {
            "score": 1.0,
            "content": "flat indexed chunk",
            "path": "/original/source.pdf",
            "filename": "source.pdf",
        }
    ]

    results = query._process_keyword_results(raw_results)

    assert results == [
        {
            "path": "/original/source.pdf",
            "lookup_path": "/original/source.pdf",
            "filename": "source.pdf",
            "snippet": "flat indexed chunk",
            "score": "1.0000",
        }
    ]


def test_process_semantic_results_keeps_multiple_hits_for_same_source_path():
    """Semantic search should show every matching chunk from a source file."""
    raw_results = [
        {
            "_distance": 0.1,
            "source": "/original/source.pdf",
            "title": "source.pdf",
            "content": "first semantic chunk",
        },
        {
            "_distance": 0.2,
            "source": "/original/source.pdf",
            "title": "source.pdf",
            "content": "second semantic chunk",
        },
    ]

    results = query._process_semantic_results(raw_results)

    assert len(results) == 2
    assert [result["snippet"] for result in results] == [
        "first semantic chunk",
        "second semantic chunk",
    ]
    assert [result["score"] for result in results] == ["0.9000", "0.8000"]
    assert {result["path"] for result in results} == {"/original/source.pdf"}


def test_process_semantic_results_hides_unavailable_zero_score():
    """Vector backends that report score=0.0 (e.g. model2vec) should not show a score.

    The real search pipeline yields ``SearchResult`` objects whose ``.score`` is 0.0 and
    whose document carries no distance, so displaying "Score: 0.0000" on every hit is
    misleading. Those results should come back with an empty score string.
    """
    raw_results = [
        SimpleNamespace(
            score=0.0,
            doc_id="row-uuid",
            document={
                "source": "/original/source.pdf",
                "title": "source.pdf",
                "content": "semantic result text",
            },
        )
    ]

    results = query._process_semantic_results(raw_results)

    assert len(results) == 1
    assert results[0]["snippet"] == "semantic result text"
    assert results[0]["score"] == ""


def test_semantic_search_omits_zero_score_badge(monkeypatch):
    """A zero/unavailable semantic score must not render a "Score:" badge in the page."""
    query._state.search_pipeline = lambda _query: [
        SimpleNamespace(
            score=0.0,
            doc_id="row-uuid",
            document={
                "source": "/original/source.pdf",
                "title": "source.pdf",
                "content": "semantic result text",
            },
        )
    ]
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post("/search", data={"query": "semantic"})

    assert response.status_code == 200
    assert "semantic result text" in response.text
    assert "Score: 0.0000" not in response.text
    assert "Score:" not in response.text


def test_semantic_search_results_match_keyword_result_display(monkeypatch):
    """Semantic results should render source path, snippet, and score like keyword results."""
    query._state.show_source_paths = True
    query._state.search_pipeline = lambda _query: [
        {
            "_distance": 0.25,
            "_doc_id": "row-uuid",
            "source": "/original/source.pdf",
            "title": "source.pdf",
            "content": "semantic result text",
        }
    ]
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post("/search", data={"query": "semantic"})

    assert response.status_code == 200
    assert 'class="result-title-link"' in response.text
    assert 'href="/source-file?path=' in response.text
    assert "semantic result text" in response.text
    assert "Score: 0.7500" in response.text
    assert "Relevance:" not in response.text


def test_semantic_search_results_hide_source_path_by_default(monkeypatch):
    """Semantic results should not show source paths unless explicitly enabled."""
    query._state.search_pipeline = lambda _query: [
        {
            "_distance": 0.25,
            "_doc_id": "row-uuid",
            "source": "/original/source.pdf",
            "title": "source.pdf",
            "content": "semantic result text",
        }
    ]
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post("/search", data={"query": "semantic"})

    assert response.status_code == 200
    assert "/original/source.pdf" not in response.text
    assert 'href="/source-file?path=' not in response.text


def test_chat_response_includes_source_citations(monkeypatch):
    """Ask responses should include display-ready source chunks for trust."""
    query._state.chat_pipeline = lambda _message: "Answer from the vault."
    query._state.search_pipeline = lambda _message: [
        {
            "_distance": 0.2,
            "_doc_id": "row-uuid",
            "source": "/notes/source.txt",
            "title": "source.txt",
            "content": "Relevant source chunk",
        }
    ]
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post("/chat", data={"message": "What matters?"})

    assert response.status_code == 200
    assert "Answer from the vault." in response.text
    assert 'id="server-citations"' in response.text
    assert "source.txt" in response.text
    assert "Relevant source chunk" in response.text
    assert "Copy Chunk" in response.text
    assert "fetch('/chunk-content?path=" in response.text
    assert '"lookup_path": "row-uuid"' in response.text


def test_chat_citations_hide_source_paths_by_default(monkeypatch):
    """The embedded citations JSON must not leak absolute paths when hidden."""
    query._state.chat_pipeline = lambda _message: "Answer from the vault."
    query._state.search_pipeline = lambda _message: [
        {
            "_distance": 0.2,
            "_doc_id": "row-uuid",
            "source": "/original/secret-dir/source.txt",
            "title": "source.txt",
            "content": "Relevant source chunk",
        }
    ]
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)

    response = client.post("/chat", data={"message": "What matters?"})

    assert response.status_code == 200
    assert "/original/secret-dir/source.txt" not in response.text
    # Chunk lookup still works through the stable row id.
    assert '"lookup_path": "row-uuid"' in response.text


def test_chat_page_includes_full_chunk_modal():
    """Ask sources should offer the same full-chunk viewer as search pages."""
    client = TestClient(query.app)

    response = client.get("/chat")

    assert response.status_code == 200
    assert 'id="document-modal"' in response.text
    assert "openDocumentModal" in response.text
    assert "View full chunk" in response.text


def test_search_pages_include_full_chunk_modal():
    """The shared chunk modal must stay present on both search pages."""
    client = TestClient(query.app)

    for path in ("/search", "/keyword-search"):
        response = client.get(path)

        assert response.status_code == 200
        assert 'id="document-modal"' in response.text
        assert "openDocumentModal" in response.text


def test_copy_buttons_work_without_secure_context():
    """Copying must not depend on the secure-context-only Clipboard API.

    navigator.clipboard is undefined on plain-http origins other than
    localhost (e.g. browsing to another machine), so every page needs the
    execCommand fallback helper available."""
    client = TestClient(query.app)

    response = client.get("/chat")

    assert response.status_code == 200
    assert "function copyTextToClipboard" in response.text
    assert "document.execCommand('copy')" in response.text
    # Chunk copies must start the clipboard write synchronously in the click
    # handler (Firefox drops writes once the click's transient user
    # activation is spent, e.g. after an awaited fetch).
    assert "function copyPendingTextToClipboard" in response.text
    assert "copyPendingTextToClipboard(textPromise)" in response.text
    # The page's copy handlers go through the fallback-aware helpers.
    assert "copyTextToClipboard(currentAnswer)" in response.text


def _chat_error_response(monkeypatch, exc: Exception) -> str:
    """Post a chat message whose pipeline raises exc; return the page text."""

    def failing_pipeline(_message):
        raise exc

    query._state.chat_pipeline = failing_pipeline
    query._state.search_pipeline = lambda _message: []
    monkeypatch.setattr(query, "_refresh_pipelines", lambda: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)
    client = TestClient(query.app)
    response = client.post("/chat", data={"message": "What matters?"})
    assert response.status_code == 200
    return response.text


def test_chat_ollama_connection_error_points_at_settings(monkeypatch):
    """Ollama connection failures should mention the in-app Settings fix."""
    body = _chat_error_response(
        monkeypatch,
        RuntimeError("Failed to connect to Ollama at 'http://localhost:11434'."),
    )
    assert "set the Ollama server URL in this app" in body
    assert "Settings &gt; Connections &amp; credentials" in body


def test_chat_missing_openai_key_error_points_at_settings(monkeypatch):
    """Missing OpenAI credentials should mention the in-app Settings fix."""
    body = _chat_error_response(
        monkeypatch,
        RuntimeError(
            "Could not initialize the OpenAI client: Missing credentials. "
            "Set the OPENAI_API_KEY environment variable."
        ),
    )
    assert "enter the API key in this app" in body
    assert "Settings &gt; Connections &amp; credentials" in body


def test_chat_missing_anthropic_key_error_points_at_settings(monkeypatch):
    """Missing Anthropic credentials should mention the in-app Settings fix."""
    body = _chat_error_response(
        monkeypatch,
        RuntimeError(
            "Could not initialize the Anthropic client: no API key provided. "
            "Set the ANTHROPIC_API_KEY environment variable."
        ),
    )
    assert "enter the API key in this app" in body


def test_vault_text_search_default_limit_returns_all_whoosh_results(
    tmp_path, monkeypatch
):
    """VaultTextSearch should not impose a low UI-facing result cap."""
    captured: dict[str, object] = {}

    class _FakeSearchWhoosh:
        def as_function(self, single_in: bool, single_out: bool):
            return lambda _value: []

    def _fake_search_whoosh(**kwargs):
        captured.update(kwargs)
        return _FakeSearchWhoosh()

    monkeypatch.setattr(searching_and_prompting, "searchWhoosh", _fake_search_whoosh)
    monkeypatch.setattr(
        searching_and_prompting,
        "get_whoosh_index_path",
        lambda _vault_path: str(tmp_path / "fulltext_vault"),
    )

    VaultTextSearch(vault_path=str(tmp_path))

    assert captured["limit"] is None


def test_chunk_content_route_finds_lancedb_row_by_id_and_middle_snippet(monkeypatch):
    """Chunk content route should resolve keyword results sent with LanceDB row ids."""
    query._state.vault_path = "/tmp/test-vault"
    full_text = "Introductory text. The matching passage appears in the middle."
    monkeypatch.setattr(
        query,
        "_load_docs_rows",
        lambda _vault_path: [
            {
                "id": "row-uuid",
                "document": {
                    "content": full_text,
                    "path": "/notes/source.txt",
                },
            }
        ],
    )
    client = TestClient(query.app)

    response = client.get(
        "/chunk-content",
        params={"path": "row-uuid", "snippet": "matching passage appears"},
    )

    assert response.status_code == 200
    assert response.json() == {"path": "row-uuid", "content": full_text}


def test_chunk_content_route_finds_lancedb_row_by_source_path(monkeypatch):
    """Chunk content route should resolve flat Whoosh results sent by source path."""
    query._state.vault_path = "/tmp/test-vault"
    full_text = "Introductory text. The matching passage appears in the middle."
    monkeypatch.setattr(
        query,
        "_load_docs_rows",
        lambda _vault_path: [
            {
                "id": "row-uuid",
                "document": {
                    "content": full_text,
                    "source": "/notes/source.txt",
                },
            }
        ],
    )
    client = TestClient(query.app)

    response = client.get(
        "/chunk-content",
        params={"path": "/notes/source.txt", "snippet": "matching passage appears"},
    )

    assert response.status_code == 200
    assert response.json() == {"path": "/notes/source.txt", "content": full_text}


def test_chunk_content_route_falls_back_to_snippet_for_stale_whoosh_id(monkeypatch):
    """Chunk content route should tolerate stale Whoosh ids when snippet matches."""
    query._state.vault_path = "/tmp/test-vault"
    full_text = "Introductory text. The matching passage appears in the middle."
    monkeypatch.setattr(
        query,
        "_load_docs_rows",
        lambda _vault_path: [
            {
                "id": "current-row-uuid",
                "document": {
                    "content": full_text,
                    "source": "/notes/source.txt",
                },
            }
        ],
    )
    client = TestClient(query.app)

    response = client.get(
        "/chunk-content",
        params={"path": "stale-whoosh-uuid", "snippet": "matching passage appears"},
    )

    assert response.status_code == 200
    assert response.json() == {"path": "stale-whoosh-uuid", "content": full_text}


def test_source_file_route_is_disabled_by_default(tmp_path):
    """Source-file endpoint should not expose files unless source paths are enabled."""
    source_file = tmp_path / "source.txt"
    source_file.write_text("source text")
    client = TestClient(query.app)

    response = client.get("/source-file", params={"path": str(source_file)})

    assert response.status_code == 404
    assert response.json() == {"error": "Source file links are disabled."}


def test_source_file_route_blocks_unreferenced_file_when_enabled(tmp_path, monkeypatch):
    """Source-file endpoint should not serve files absent from the vault index."""
    query._state.show_source_paths = True
    source_file = tmp_path / "source.txt"
    source_file.write_text("source text")
    monkeypatch.setattr(query, "_indexed_source_paths", lambda _vault_path: set())
    client = TestClient(query.app)

    response = client.get("/source-file", params={"path": str(source_file)})

    assert response.status_code == 404
    assert response.json() == {"error": "Source file is not referenced by this vault."}


def test_source_file_route_serves_referenced_file_when_enabled(tmp_path, monkeypatch):
    """Source-file endpoint should serve only files referenced by the vault index."""
    query._state.show_source_paths = True
    source_file = tmp_path / "source.txt"
    source_file.write_text("source text")
    monkeypatch.setattr(
        query,
        "_indexed_source_paths",
        lambda _vault_path: {str(source_file)},
    )
    client = TestClient(query.app)

    response = client.get("/source-file", params={"path": str(source_file)})

    assert response.status_code == 200
    assert response.text == "source text"


def test_indexed_source_paths_reads_references_from_docs_rows(monkeypatch):
    """Allowed source-file paths should come from indexed docs-row metadata."""
    monkeypatch.setattr(
        query,
        "_load_docs_rows",
        lambda _vault_path: [
            {
                "id": "row-id",
                "document": {
                    "content": "text",
                    "source": "/indexed/source.pdf",
                    "id": "/indexed/source.pdf",
                },
            },
            {"id": "row-id-2", "document": {"path": "/indexed/other.txt"}},
        ],
    )

    assert query._indexed_source_paths("/tmp/test-vault") == {
        "/indexed/source.pdf",
        "/indexed/other.txt",
    }


def test_apply_vault_embedding_config_restores_recorded(tmp_path):
    """Opening a vault should switch the active embedder to its recorded one."""
    from talkpipe_vault.pipelines import vault_metadata

    vault_metadata.record_embedding_config(
        str(tmp_path),
        source="openai",
        model="text-embedding-3-large",
        dimension=3072,
    )
    query._state.embedding_source = "leftover"
    query._state.embedding_model = "leftover"

    query._apply_vault_embedding_config(str(tmp_path))

    assert query._state.embedding_source == "openai"
    assert query._state.embedding_model == "text-embedding-3-large"


def test_apply_vault_embedding_config_legacy_uses_saved_override(tmp_path):
    """A legacy vault (no record) falls back to the saved override, not leftovers."""
    from talkpipe_vault.apps import user_settings

    user_settings.save_model_overrides(
        embedding_source="ollama", embedding_model="nomic-embed-text"
    )
    # Simulate a different vault having been opened just before.
    query._state.embedding_source = "openai"
    query._state.embedding_model = "text-embedding-3-large"

    query._apply_vault_embedding_config(str(tmp_path))

    assert query._state.embedding_source == "ollama"
    assert query._state.embedding_model == "nomic-embed-text"


def test_apply_vault_embedding_config_legacy_without_override_is_none(tmp_path):
    """A legacy vault with no saved override leaves the embedder unset (default)."""
    query._state.embedding_source = "openai"
    query._state.embedding_model = "text-embedding-3-large"

    query._apply_vault_embedding_config(str(tmp_path))

    assert query._state.embedding_source is None
    assert query._state.embedding_model is None


def test_load_saved_model_overrides_drops_unavailable_source():
    """A saved provider that is no longer installed must not brick startup.

    The stale source/model pair falls back to None (TalkPipe config/defaults)
    so init_pipelines can still build pipelines and the server boots.
    """
    from talkpipe_vault.apps import user_settings

    user_settings.save_model_overrides(
        embedding_source="uninstalled-plugin-source",
        embedding_model="some-model",
        chat_source="ollama",
        chat_model="mistral-small",
    )

    query.load_saved_model_overrides()

    assert query._state.embedding_source is None
    assert query._state.embedding_model is None
    # The valid chat override is untouched.
    assert query._state.chat_source == "ollama"
    assert query._state.chat_model == "mistral-small"


def test_load_saved_model_overrides_keeps_available_source():
    """Valid saved overrides keep working exactly as before."""
    from talkpipe_vault.apps import user_settings

    user_settings.save_model_overrides(
        embedding_source="model2vec",
        embedding_model="minishlab/potion-retrieval-32M",
    )

    query.load_saved_model_overrides()

    assert query._state.embedding_source == "model2vec"
    assert query._state.embedding_model == "minishlab/potion-retrieval-32M"


def test_apply_vault_embedding_config_ignores_unavailable_recorded_source(
    tmp_path, monkeypatch
):
    """A vault recorded with a now-missing embedder falls back instead of raising."""
    monkeypatch.setattr(
        query.vault_metadata,
        "load_embedding_config",
        lambda path: {"source": "uninstalled-plugin-source", "model": "some-model"},
    )
    query._state.embedding_source = "leftover"
    query._state.embedding_model = "leftover"

    query._apply_vault_embedding_config(str(tmp_path))

    assert query._state.embedding_source is None
    assert query._state.embedding_model is None


def test_record_vault_embedding_config_writes_sidecar(tmp_path, monkeypatch):
    """Recording should persist source/model (and probed dimension) to the vault."""
    from talkpipe_vault.pipelines import vault_metadata

    monkeypatch.setattr(
        query.vault_metadata, "probe_embedding_dimension", lambda source, model: 256
    )
    query._record_vault_embedding_config(
        str(tmp_path), "model2vec", "minishlab/potion-retrieval-32M"
    )
    embedding = vault_metadata.load_embedding_config(str(tmp_path))
    assert embedding["source"] == "model2vec"
    assert embedding["model"] == "minishlab/potion-retrieval-32M"
    assert embedding["dimension"] == 256


def test_config_status_reports_embedding_mismatch(tmp_path, monkeypatch):
    """The config-status endpoint flags a vault indexed with a different embedder."""
    from talkpipe_vault.pipelines import diagnostics, vault_metadata

    vault_metadata.record_embedding_config(
        str(tmp_path), source="openai", model="text-embedding-3-large"
    )
    query._state.vault_path = str(tmp_path)
    # Current embedder differs from what the vault was indexed with.
    query._state.embedding_source = "model2vec"
    query._state.embedding_model = "minishlab/potion-retrieval-32M"
    monkeypatch.setattr(
        diagnostics,
        "_functional_probe",
        lambda role, key, model, timeout: (True, 384),
    )
    client = TestClient(query.app)

    body = client.get("/api/config-status").json()
    match = next(c for c in body["checks"] if c["name"] == "Embedding ↔ index")
    assert match["status"] == "error"
    assert "text-embedding-3-large" in match["summary"]
