from tracemalloc import Filter
from typing import Annotated, Optional, Any
from talkpipe import segment, register_segment, source, register_source
from talkpipe.pipe.io import Print
from talkpipe.data.extraction import listFiles
from talkpipe.pipelines.vector_databases import MakeVectorDatabaseSegment
from talkpipe_vault.watchdog import file_watcher
from talkpipe_vault.docling import DoclingFileToText
from talkpipe.pipe.basic import ToDict, FilterExpression, EvalExpression, setAs, fillTemplate
from talkpipe.data.text.chunking_units import splitText, ShingleText
from talkpipe.search.whoosh import indexWhoosh
from .config import EMBEDDING_MODEL, EMBEDDING_SOURCE, DOCUMENT_TEMPLATE, SHINGLE_TEMPLATE


@register_segment("buildVectorDBFromPaths")
@segment()
def build_vector_db_from_paths(items: Any,
                               vault_path: Annotated[str, "Base path for vault storage. Vector DB stored at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"],
                               overwrite: Annotated[bool, "If true, overwrite existing tables and indexes"] = False):
    """
    Segment that builds a vector database and full-text search index from file paths.

    Expects input items as dicts with the following structure:
        - "path": str - File path to process
        - "event": str (optional) - If present and equals "deleted", item is skipped

    Processes each file through Docling for text extraction, then creates embeddings
    for both full documents and shingled chunks. Stores results in two LanceDB tables:
    'full_documents' and 'shingled_chunks'. Also indexes full documents in a Whoosh
    full-text search index for keyword-based retrieval.

    Storage locations derived from vault_path:
        - vault_path/vector_vault: LanceDB vector database
        - vault_path/fulltext_vault: Whoosh full-text search index

    Yields dicts with the following structure:
        - "id": str - Unique chunk identifier (paragraph range + path)
        - "shingle": str - Templated text chunk content
        - "path": str - Source file path
    """
    import os
    vectordb_path = os.path.join(vault_path, "vector_vault")
    whoosh_index_path = os.path.join(vault_path, "fulltext_vault")

    pipeline = \
    FilterExpression(expression="'event' not in item or item['event'] != 'deleted'") | \
    DoclingFileToText(
        field="path",
        set_as="full_content") | \
    FilterExpression(expression="item.get('full_content') and len(item.get('full_content', '').strip()) > 0") | \
    fillTemplate(template=DOCUMENT_TEMPLATE, set_as="doc_save_query") | \
    MakeVectorDatabaseSegment(
        path=vectordb_path,
        embedding_model=EMBEDDING_MODEL,
        embedding_source=EMBEDDING_SOURCE,
        embedding_field="doc_save_query",
        table_name="full_documents",
        doc_id_field="path",
        overwrite=overwrite) | \
    setAs(field_list="path:doc_id") | \
    indexWhoosh(
        index_path=whoosh_index_path,
        field_list="full_content:content",
        overwrite=overwrite) | \
    ToDict(field_list="path,full_content") | \
    splitText(field="full_content", criteria=500, set_as="chunk") | \
    FilterExpression(expression="item.get('chunk') and len(item.get('chunk', '').strip()) > 0") | \
    ShingleText(field="chunk", shingle_size=3, overlap=1, set_as="shingle_detail", key="path", emit_detail=True) | \
    setAs(field_list="shingle_detail.text:shingle") | \
    EvalExpression(set_as="id", expression="""str(item['shingle_detail']['first_paragraph'])+'-'+str(item['shingle_detail']['last_paragraph'])+'-'+str(item['path'])""") | \
    FilterExpression(expression="item.get('shingle') and len(item.get('shingle', '').strip()) > 0") | \
    ToDict(field_list="id,shingle,path") | \
    fillTemplate(template=SHINGLE_TEMPLATE, set_as="shingle") | \
    MakeVectorDatabaseSegment(
        path=vectordb_path,
        embedding_model=EMBEDDING_MODEL,
        embedding_source=EMBEDDING_SOURCE,
        embedding_field="shingle",
        table_name="shingled_chunks",
        doc_id_field="id",
        overwrite=False) | \
    ToDict(field_list="id,shingle,path")
    yield from pipeline(items)


@register_source("watchIntoVectorDB")
@source()
def watch_into_vector_db(source_path: Annotated[str, "Path to watch"],
                         vault_path: Annotated[str, "Base path for vault storage. Vector DB stored at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"],
                         patterns: Annotated[list[str] | None, "List of glob patterns to match"] = None,
                         ignore_patterns: Annotated[list[str] | None, "List of glob patterns to ignore"] = None,
                         ignore_directories: Annotated[bool, "Whether to ignore directory events"] = True,
                         case_sensitive: Annotated[bool, "Whether pattern matching is case-sensitive"] = False,
                         max_events: Annotated[int | None, "Maximum number of events to process"] = None,
                         polling: Annotated[bool, "Use polling-based observer"] = False,
                         ignore_common: Annotated[bool, "Ignore common temp/hidden files"] = True,
                         overwrite: Annotated[bool, "If true, overwrite existing tables and indexes"] = False,):
    """
    Source that watches a directory and processes file changes into a vector database and full-text index.

    Combines file watching with vector database and Whoosh full-text index building.
    Monitors the source directory for file events and automatically processes
    new/modified files into LanceDB tables and Whoosh index.

    Yields dicts with the following structure:
        - "id": str - Unique chunk identifier
        - "shingle": str - Templated text chunk content
        - "path": str - Source file path
    """
    pipeline = file_watcher(
        path=source_path,
        patterns=patterns,
        ignore_patterns=ignore_patterns,
        ignore_directories=ignore_directories,
        case_sensitive=case_sensitive,
        max_events=max_events,
        polling=polling,
        ignore_common=ignore_common) | \
    Print() | \
    build_vector_db_from_paths(
        vault_path=vault_path,
        overwrite=overwrite)
    yield from pipeline()

@register_source("listIntoVectorDB")
@source()
def list_into_vector_db(source_pattern: Annotated[str, "Glob pattern to match files (e.g., '/path/**/*.pdf')"],
                         vault_path: Annotated[str, "Base path for vault storage. Vector DB stored at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"],
                         overwrite: Annotated[bool, "If true, overwrite existing tables and indexes"] = False,):
    """
    Source that batch processes files matching a glob pattern into a vector database and full-text index.

    Lists all files matching the source pattern and processes them into LanceDB
    and Whoosh full-text index. Useful for initial bulk loading of documents into the vault.

    Yields dicts with the following structure:
        - "id": str - Unique chunk identifier
        - "shingle": str - Templated text chunk content
        - "path": str - Source file path
    """
    pipeline = listFiles(full_path=True, files_only=True) | \
    ToDict(field_list="_:path") | \
    Print() | \
    build_vector_db_from_paths(
        vault_path=vault_path,
        overwrite=overwrite)
    yield from pipeline([source_pattern])