"""Unit tests for the vault query web app."""

import pytest
from fastapi.testclient import TestClient

from talkpipe_vault.apps import query
from talkpipe_vault.pipelines import searching_and_prompting
from talkpipe_vault.pipelines.searching_and_prompting import VaultTextSearch


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
def reset_app_state(tmp_path):
    """Reset global query app state between tests."""
    query._state.vault_path = str(tmp_path)
    query._state.search_pipeline = None
    query._state.chat_pipeline = None
    query._state.keyword_search_pipeline = None
    query._state.shingled_chunks_count = 0
    query._state.fulltext_documents_count = 0
    query._state.keyword_search_enabled = False
    query._state.show_source_paths = False
    query._state.last_refresh_time = 0.0
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


def test_create_whoosh_index_route_triggers_builder(monkeypatch, capsys):
    """Create-index endpoint should build index and redirect with success message."""
    query._state.vault_path = "/tmp/test-vault"
    monkeypatch.setattr(
        query,
        "_iter_lancedb_docs_for_whoosh",
        lambda _vault_path: [
            {
                "doc_id": "1",
                "content": "abc",
                "path": "a.txt",
                "filename": "a.txt",
            }
        ],
    )
    monkeypatch.setattr(
        query,
        "get_whoosh_index_path",
        lambda _vault_path: "/tmp/test-vault/fulltext_vault",
    )
    called: dict[str, object] = {}

    def _fake_index_whoosh(**kwargs):
        called["kwargs"] = kwargs

        def _runner(items):
            called["items"] = list(items)
            return []

        return _runner

    monkeypatch.setattr(query, "indexWhoosh", _fake_index_whoosh)
    monkeypatch.setattr(query, "_refresh_pipelines", lambda force=False: None)
    monkeypatch.setattr(query, "_update_document_counts", lambda vault_path: None)

    client = TestClient(query.app)
    response = client.post("/keyword-search/create-index", follow_redirects=False)

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/keyword-search?created=Whoosh%20index%20created."
    )
    assert called["kwargs"] == {
        "index_path": "/tmp/test-vault/fulltext_vault",
        "field_list": "content:content,path:path,filename:filename,doc_id:doc_id",
        "overwrite": True,
        "commit_seconds": 0,
    }
    assert called["items"] == [
        {"doc_id": "1", "content": "abc", "path": "a.txt", "filename": "a.txt"}
    ]
    output = capsys.readouterr().out
    assert "Indexing 1 document(s) into the Whoosh full-text index." in output
    assert "doc_id: 1" in output
    assert "path: a.txt" in output
    assert "content:\nabc" in output


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
    monkeypatch.setattr(query, "ensure_supported_vault_layout", lambda _vault_path: None)

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
