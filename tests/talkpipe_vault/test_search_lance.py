"""Unit tests for searchLance keyword search segment."""

from talkpipe.search.lancedb import add_to_lancedb

from talkpipe_vault.pipelines.searching_and_prompting import searchLance


def test_search_lance_returns_keyword_matches(tmp_path):
    """searchLance should return scored matches from LanceDB documents."""
    items = [
        {
            "vector": [0.1, 0.2],
            "id": "doc-1",
            "content": "FastAPI is a modern Python framework.",
            "path": "doc-1.txt",
            "title": "Doc 1",
        },
        {
            "vector": [0.3, 0.4],
            "id": "doc-2",
            "content": "Machine learning uses data and models.",
            "path": "doc-2.txt",
            "title": "Doc 2",
        },
    ]
    list(
        add_to_lancedb(
            path=str(tmp_path),
            table_name="docs",
            vector_field="vector",
            doc_id_field="id",
        )(items)
    )

    results_by_query = list(
        searchLance(
            path=str(tmp_path),
            table_name="docs",
            all_results_at_once=True,
            limit=10,
        )(["FastAPI"])
    )

    assert len(results_by_query) == 1
    results = results_by_query[0]
    assert len(results) == 1
    assert results[0]["doc_id"] == "doc-1"
    assert results[0]["score"] > 0
    assert results[0]["document"]["path"] == "doc-1.txt"
