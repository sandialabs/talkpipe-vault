"""Reading a named vault's stores: the LanceDB docs table and the Whoosh index.

Row streaming, chunk and source-file lookup by a result's key, the
full-text index check and rebuild. Every function takes the vault path;
none touches application state.
"""

import logging
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshFullTextIndex

from talkpipe_vault.pipelines.config import (
    DEFAULT_VECTOR_TABLE_NAME,
    WHOOSH_INDEX_FIELDS,
    ensure_supported_vault_layout,
    get_vector_db_path,
    get_whoosh_index_path,
)
from talkpipe_vault.pipelines.search_results import normalize_document_cell

logger = logging.getLogger(__name__)


def _keyword_search_enabled(vault_path: str) -> bool:
    """Return True when the vault has a readable Whoosh full-text index."""
    if not vault_path:
        return False

    whoosh_index_path = get_whoosh_index_path(vault_path)

    # Opening WhooshFullTextIndex creates the index directory as a side
    # effect, which would leave every vault with a confusing empty
    # fulltext_vault/ folder just from checking whether keyword search is
    # available. Only probe when the directory already exists.
    if not os.path.isdir(whoosh_index_path):
        return False

    try:
        with WhooshFullTextIndex(whoosh_index_path):
            return True
    except Exception:
        return False


def _iter_lancedb_docs_for_whoosh(vault_path: str) -> list[dict[str, str]]:
    """
    Read docs-table records from LanceDB and normalize for Whoosh indexing.

    Expects rows where `document` is either JSON text or a dict containing content/path/title.
    """
    documents: list[dict[str, str]] = []
    for row in _load_docs_rows(vault_path):
        parsed_doc = _extract_document_record(row)
        path = str(
            parsed_doc.get("path")
            or parsed_doc.get("source")
            or parsed_doc.get("id")
            or row.get("id", "")
        )
        filename = str(
            parsed_doc.get("filename")
            or parsed_doc.get("title")
            or (Path(path).name if path else "")
        )
        documents.append(
            {
                "doc_id": str(row.get("id", path)),
                "content": str(parsed_doc.get("content", "")),
                "path": path,
                "filename": filename,
            }
        )

    return documents


def _build_whoosh_index(
    vault_path: str,
    documents: list[dict[str, str]],
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Rebuild the vault's Whoosh full-text index from normalized documents.

    Expects documents as dicts with doc_id, content, path, and filename keys.
    The existing index is replaced. Documents keep their LanceDB row ids as
    Whoosh doc_ids so search results can be resolved back to stored chunks.
    (talkpipe's indexWhoosh segment reserves the doc_id schema field, so the
    index is built through WhooshFullTextIndex to control ids directly.)
    The optional progress callback receives (docs_done, total_docs) after each
    document is added.
    """
    whoosh_index_path = get_whoosh_index_path(vault_path)
    if os.path.isdir(whoosh_index_path):
        shutil.rmtree(whoosh_index_path)

    total = len(documents)
    # One writer committed once for the whole rebuild: add_document commits
    # per call, which makes Whoosh re-merge its segments on every document
    # and degrades quadratically with vault size.
    with (
        WhooshFullTextIndex(whoosh_index_path, fields=WHOOSH_INDEX_FIELDS) as ix,
        ix.ix.writer() as writer,
    ):
        for done, document in enumerate(documents, start=1):
            writer.update_document(
                doc_id=document.get("doc_id") or str(uuid.uuid4()),
                **{
                    field: str(document.get(field, "")) for field in WHOOSH_INDEX_FIELDS
                },
            )
            if progress is not None:
                progress(done, total)


def _extract_document_record(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a LanceDB docs-row into a dict-like document."""
    record: dict[str, Any] = normalize_document_cell(row.get("document", {}))
    return record


def _load_docs_rows(vault_path: str) -> Iterator[dict[str, Any]]:
    """Stream id/document rows from the vault's TalkPipe docs table.

    Reads column-pruned batches instead of materializing the table:
    ``to_arrow()`` holds every row's embedding vector in memory at once —
    gigabytes on a large vault — and no caller needs the vectors.
    """
    ensure_supported_vault_layout(vault_path)
    vectordb_path = get_vector_db_path(vault_path)
    doc_store = LanceDBDocumentStore(
        path=vectordb_path,
        table_name=DEFAULT_VECTOR_TABLE_NAME,
    )
    table, _ = doc_store._get_table()
    if hasattr(table, "search"):
        query = table.search(None).select(["id", "document"]).limit(None)
        for batch in query.to_batches():
            yield from batch.to_pylist()
        return
    # Fallback for table objects without the query API (e.g. test doubles).
    if hasattr(table, "to_arrow"):
        yield from table.to_arrow().to_pylist()
        return
    if hasattr(table, "to_pandas"):
        yield from table.to_pandas().to_dict(orient="records")
        return
    raise RuntimeError(
        "Unsupported LanceDB table reader: expected to_arrow() or to_pandas()."
    )


def _normalize_snippet_prefix(snippet: str) -> str:
    """Normalize UI snippet text for matching against stored chunk content."""
    normalized = " ".join(snippet.split())
    if normalized.endswith("..."):
        normalized = normalized[:-3].rstrip()
    return normalized


def _document_lookup_keys(row: dict[str, Any], doc: dict[str, Any]) -> set[str]:
    """Return non-empty identifiers that may be used to look up a document."""
    keys = {
        str(doc.get("path") or ""),
        str(doc.get("source") or ""),
        str(doc.get("id") or ""),
        str(row.get("id") or ""),
    }
    return {key for key in keys if key}


def _indexed_source_paths(vault_path: str) -> set[str]:
    """Return source file paths referenced by the current vault index."""
    rows = _load_docs_rows(vault_path)
    source_paths: set[str] = set()
    for row in rows:
        doc = _extract_document_record(row)
        for key in ("path", "source", "id"):
            value = str(doc.get(key) or "")
            if value:
                source_paths.add(value)
    return source_paths


def _resolve_indexed_source_file(vault_path: str, key: str) -> Path | None:
    """Resolve a result lookup key (row id or indexed path) to its source file.

    Search results and Ask citations identify chunks by ``lookup_path`` — the
    stable docs-row id when one exists, the indexed path otherwise. Either kind
    of key is matched against the docs table and mapped back to the source file
    path recorded at indexing time.
    """
    for row in _load_docs_rows(vault_path):
        doc = _extract_document_record(row)
        if key not in _document_lookup_keys(row, doc):
            continue
        source = (
            str(doc.get("path") or "")
            or str(doc.get("source") or "")
            or str(doc.get("id") or "")
        )
        if source:
            return Path(source)
    return None


def _get_chunk_text_for_path_and_snippet(
    vault_path: str, path: str, snippet: str
) -> str:
    """Return full text for a selected chunk identified by path/id and snippet."""
    rows = _load_docs_rows(vault_path)
    snippet_prefix = _normalize_snippet_prefix(snippet)
    first_snippet_match = ""
    for row in rows:
        doc = _extract_document_record(row)
        content = str(doc.get("content", "")).strip()
        if not content:
            continue
        snippet_matches = snippet_prefix and snippet_prefix in " ".join(content.split())
        if snippet_matches and not first_snippet_match:
            first_snippet_match = content
        if path not in _document_lookup_keys(row, doc):
            continue
        if snippet_prefix and not snippet_matches:
            continue
        return content
    return first_snippet_match


def _vault_has_documents(vault_path: str) -> bool:
    """True when the vault's docs table exists and holds at least one chunk.

    Any failure to read the table (no table yet, unreadable vault) counts as
    "no documents": for the embedding↔index diagnostic that is the state where
    no recorded config should be expected.
    """
    try:
        doc_store = LanceDBDocumentStore(
            path=get_vector_db_path(vault_path),
            table_name=DEFAULT_VECTOR_TABLE_NAME,
        )
        return bool(doc_store.count() > 0)
    except Exception:
        return False
