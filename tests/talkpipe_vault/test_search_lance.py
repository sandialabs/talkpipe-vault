"""Unit tests for searchLance keyword search segment."""

import json
from typing import Any

from talkpipe_vault.pipelines import searching_and_prompting
from talkpipe_vault.pipelines.searching_and_prompting import searchLance


class _FakeSearch:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.limit_value: int | None = None

    def limit(self, value: int) -> "_FakeSearch":
        self.limit_value = value
        return self

    def to_list(self) -> list[dict[str, Any]]:
        return self.rows[: self.limit_value]


class _FakeIndex:
    index_type = "FTS"
    columns = ["document"]


class _FakeTable:
    def __init__(self, rows: list[dict[str, Any]], has_fts_index: bool):
        self.rows = rows
        self.has_fts_index = has_fts_index
        self.create_fts_index_called = False
        self.search_calls: list[tuple[str, str | None]] = []

    def list_indices(self) -> list[_FakeIndex]:
        return [_FakeIndex()] if self.has_fts_index else []

    def create_fts_index(self, *args: Any, **kwargs: Any) -> None:
        self.create_fts_index_called = True
        raise AssertionError("searchLance must not create FTS indexes")

    def search(self, query: str, query_type: str | None = None) -> _FakeSearch:
        self.search_calls.append((query, query_type))
        return _FakeSearch(self.rows)


class _FakeDocStore:
    def __init__(self, table: _FakeTable):
        self.table = table

    def _get_table(self) -> tuple[_FakeTable, None]:
        return self.table, None


def _patch_doc_store(monkeypatch: Any, table: _FakeTable) -> None:
    monkeypatch.setattr(
        searching_and_prompting,
        "LanceDBDocumentStore",
        lambda **_: _FakeDocStore(table),
    )


def test_search_lance_returns_keyword_matches_when_fts_exists(monkeypatch):
    """searchLance should use an existing LanceDB FTS index."""
    table = _FakeTable(
        rows=[
            {
                "id": "doc-1",
                "_score": 2.5,
                "document": json.dumps(
                    {
                        "content": "FastAPI is a modern Python framework.",
                        "path": "doc-1.txt",
                    }
                ),
            }
        ],
        has_fts_index=True,
    )
    _patch_doc_store(monkeypatch, table)

    results_by_query = list(
        searchLance(
            path="/tmp/vault",
            table_name="docs",
            all_results_at_once=True,
            limit=10,
        )(["FastAPI"])
    )

    assert len(results_by_query) == 1
    results = results_by_query[0]
    assert len(results) == 1
    assert results[0]["doc_id"] == "doc-1"
    assert results[0]["score"] == 2.5
    assert results[0]["document"]["path"] == "doc-1.txt"
    assert table.search_calls == [("FastAPI", "fts")]
    assert not table.create_fts_index_called


def test_search_lance_disables_keyword_search_without_fts(monkeypatch):
    """searchLance should emit empty results when no FTS index exists."""
    table = _FakeTable(rows=[], has_fts_index=False)
    _patch_doc_store(monkeypatch, table)

    results_by_query = list(
        searchLance(
            path="/tmp/vault",
            table_name="docs",
            all_results_at_once=True,
            limit=10,
        )(["FastAPI"])
    )

    assert results_by_query == [[]]
    assert table.search_calls == []
    assert not table.create_fts_index_called
