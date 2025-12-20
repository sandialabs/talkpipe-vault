import os
from typing import Annotated, Any
from talkpipe import segment, register_segment, source, register_source
from talkpipe.pipe.io import Print
from talkpipe.data.extraction import listFiles, ReadFile
from talkpipe.pipelines.vector_databases import MakeVectorDatabaseSegment
from talkpipe_vault.watchdog import file_watcher
from talkpipe.pipe.basic import ToDict, FilterExpression, EvalExpression, setAs, fillTemplate, Debounce
from talkpipe.pipe.io import FileExistsFilter, DeleteFile
from talkpipe.util.data_manipulation import extract_property
from talkpipe.data.text.chunking_units import splitText, ShingleText
from talkpipe.search.whoosh import indexWhoosh
from .config import EMBEDDING_MODEL, EMBEDDING_SOURCE, DOCUMENT_TEMPLATE, SHINGLE_TEMPLATE

_LANCEDB_BATCH_SIZE = 1000

@register_segment("buildVectorDBFromPaths")
@segment()
def build_vector_db_from_paths(items: Any,
                               vault_path: Annotated[str, "Base path for vault storage. Vector DB stored at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"],
                               overwrite: Annotated[bool, "If true, overwrite existing tables and indexes"] = False,
                               delete_after_reading: Annotated[bool, "If true, delete source files after successfully indexing them"] = False):
    """
    Segment that builds a vector database and full-text search index from file paths.

    Expects input items as dicts with the following structure:
        - "path": str - File path to process
        - "event": str (optional) - If present and equals "deleted", item is skipped

    Processes each file through readFile (using docling for documents) to extract text
    as ExtractionResult objects, then creates embeddings for both full documents and
    shingled chunks. Stores results in two LanceDB tables: 'full_documents' and
    'shingled_chunks'. Also indexes full documents in a Whoosh full-text search index
    for keyword-based retrieval.

    Storage locations derived from vault_path:
        - vault_path/vector_vault: LanceDB vector database
        - vault_path/fulltext_vault: Whoosh full-text search index

    Yields dicts with the following structure:
        - "shingle_id": str - Unique chunk identifier (paragraph range + source path)
        - "shingle": str - Templated text chunk content
        - "source": str - Source file path
        - "title": str - Source file name (from ExtractionResult)
    """
    vectordb_path = os.path.join(vault_path, "vector_vault")
    whoosh_index_path = os.path.join(vault_path, "fulltext_vault")

    # Build the base pipeline
    base_pipeline = \
    FilterExpression(expression="'event' not in item or item['event'] != 'deleted'") | \
    FileExistsFilter(path_field="path") | \
    ReadFile(field="path") 

    # Conditionally add file deletion after successful indexing
    if delete_after_reading:
        base_pipeline = base_pipeline | DeleteFile(path_field="source")

    pipeline = base_pipeline | \
    ToDict(field_list="content,source,id,title") | \
    FilterExpression(expression="item.get('content') and len(item.get('content', '').strip()) > 0") | \
    fillTemplate(template=DOCUMENT_TEMPLATE, set_as="doc_save_query") | \
    MakeVectorDatabaseSegment(
        path=vectordb_path,
        embedding_model=EMBEDDING_MODEL,
        embedding_source=EMBEDDING_SOURCE,
        embedding_field="doc_save_query",
        table_name="full_documents",
        doc_id_field="id",
        overwrite=overwrite,
        fail_on_error=False,
        batch_size=_LANCEDB_BATCH_SIZE,
        optimize_on_batch=True) | \
    setAs(field_list="id:doc_id") | \
    indexWhoosh(
        index_path=whoosh_index_path,
        field_list="content:content,source:path,title:filename",
        overwrite=overwrite,
        commit_seconds=120) | \
    ToDict(field_list="id,content,title,source") | \
    splitText(field="content", criteria=500, set_as="chunk") | \
    FilterExpression(expression="item.get('chunk') and len(item.get('chunk', '').strip()) > 0") | \
    ShingleText(field="chunk", shingle_size=3, overlap=1, set_as="shingle_detail", key="id", emit_detail=True) | \
    setAs(field_list="shingle_detail.text:shingle") | \
    EvalExpression(set_as="shingle_id", expression="""str(item['shingle_detail']['first_paragraph'])+'-'+str(item['shingle_detail']['last_paragraph'])+'-'+str(item['source'])""") | \
    FilterExpression(expression="item.get('shingle') and len(item.get('shingle', '').strip()) > 0") | \
    ToDict(field_list="shingle_id,shingle,source,title") | \
    fillTemplate(template=SHINGLE_TEMPLATE, set_as="shingle") | \
    MakeVectorDatabaseSegment(
        path=vectordb_path,
        embedding_model=EMBEDDING_MODEL,
        embedding_source=EMBEDDING_SOURCE,
        embedding_field="shingle",
        table_name="shingled_chunks",
        doc_id_field="shingle_id",
        overwrite=False,
        fail_on_error=False,
        batch_size=_LANCEDB_BATCH_SIZE,
        optimize_on_batch=True) | \
    ToDict(field_list="shingle_id,shingle,source,title")
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
                         overwrite: Annotated[bool, "If true, overwrite existing tables and indexes"] = False,
                         delete_after_reading: Annotated[bool, "If true, delete source files after successfully indexing them"] = False,
                         debounce_seconds: Annotated[float, "Seconds to wait for file stability before processing (0 to disable)"] = 1.0):
    """
    Source that watches a directory and processes file changes into a vector database and full-text index.

    Combines file watching with vector database and Whoosh full-text index building.
    Monitors the source directory for file events and automatically processes
    new/modified files into LanceDB tables and Whoosh index.

    Uses debouncing to handle race conditions where files are created and then
    immediately modified (e.g., by external processes writing content). The debounce
    ensures processing only happens after the file has stabilized.

    Yields dicts with the following structure:
        - "shingle_id": str - Unique chunk identifier
        - "shingle": str - Templated text chunk content
        - "source": str - Source file path
        - "title": str - Source file name
    """
    watcher = file_watcher(
        path=source_path,
        patterns=patterns,
        ignore_patterns=ignore_patterns,
        ignore_directories=ignore_directories,
        case_sensitive=case_sensitive,
        max_events=max_events,
        polling=polling,
        ignore_common=ignore_common)

    if debounce_seconds > 0:
        pipeline = watcher | \
        Debounce(key_field="path", debounce_seconds=debounce_seconds) | \
        Print() | \
        build_vector_db_from_paths(
            vault_path=vault_path,
            overwrite=overwrite,
            delete_after_reading=delete_after_reading)
    else:
        pipeline = watcher | \
        Print() | \
        build_vector_db_from_paths(
            vault_path=vault_path,
            overwrite=overwrite,
            delete_after_reading=delete_after_reading)

    yield from pipeline()

@register_source("listIntoVectorDB")
@source()
def list_into_vector_db(source_pattern: Annotated[str, "Glob pattern to match files (e.g., '/path/**/*.pdf')"],
                         vault_path: Annotated[str, "Base path for vault storage. Vector DB stored at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"],
                         overwrite: Annotated[bool, "If true, overwrite existing tables and indexes"] = False,
                         delete_after_reading: Annotated[bool, "If true, delete source files after successfully indexing them"] = False):
    """
    Source that batch processes files matching a glob pattern into a vector database and full-text index.

    Lists all files matching the source pattern and processes them into LanceDB
    and Whoosh full-text index. Useful for initial bulk loading of documents into the vault.

    Yields dicts with the following structure:
        - "shingle_id": str - Unique chunk identifier
        - "shingle": str - Templated text chunk content
        - "source": str - Source file path
        - "title": str - Source file name
    """
    pipeline = listFiles(full_path=True, files_only=True) | \
    ToDict(field_list="_:path") | \
    Print() | \
    build_vector_db_from_paths(
        vault_path=vault_path,
        overwrite=overwrite,
        delete_after_reading=delete_after_reading)
    yield from pipeline([source_pattern])