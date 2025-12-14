import os
import time
import threading
from queue import Queue, Empty
from typing import Annotated, Any
from talkpipe import segment, register_segment, source, register_source
from talkpipe.pipe.io import Print
from talkpipe.data.extraction import listFiles, ReadFile
from talkpipe.pipelines.vector_databases import MakeVectorDatabaseSegment
from talkpipe_vault.watchdog import file_watcher
from talkpipe.pipe.basic import ToDict, FilterExpression, EvalExpression, setAs, fillTemplate
from talkpipe.data.text.chunking_units import splitText, ShingleText
from talkpipe.search.whoosh import indexWhoosh
from .config import EMBEDDING_MODEL, EMBEDDING_SOURCE, DOCUMENT_TEMPLATE, SHINGLE_TEMPLATE


@register_segment("debounce")
@segment()
def Debounce(items: Any,
             key_field: Annotated[str, "Field name to use as the debounce key"] = "path",
             debounce_seconds: Annotated[float, "Seconds to wait for stability before yielding"] = 1.0):
    """
    Segment that debounces events by a key field, waiting for stability before yielding.

    Expects input items as dicts with a field to use as the debounce key (default: "path").
    When multiple events arrive for the same key, only the last event is yielded after
    no new events have arrived for that key within the debounce period.

    This is useful for handling race conditions where files are created and then
    immediately modified (e.g., create with 0 bytes, then write content). The debounce
    ensures processing only happens after the file has stabilized.

    Yields the most recent event for each key after the debounce period expires.
    """
    pending = {}  # key -> (item, timestamp)
    lock = threading.Lock()
    output_queue = Queue()
    stop_event = threading.Event()
    input_done = threading.Event()

    def add_pending(item):
        key = item.get(key_field) if isinstance(item, dict) else None
        if key is None:
            output_queue.put(item)
            return
        with lock:
            pending[key] = (item, time.time())

    def input_consumer():
        try:
            for item in items:
                if stop_event.is_set():
                    break
                add_pending(item)
        finally:
            input_done.set()

    def checker():
        while not stop_event.is_set():
            time.sleep(0.1)
            now = time.time()
            with lock:
                stable_keys = [
                    k for k, (item, ts) in pending.items()
                    if now - ts >= debounce_seconds
                ]
                for k in stable_keys:
                    item, _ = pending.pop(k)
                    output_queue.put(item)

            # Signal completion when input is done and no pending items
            if input_done.is_set():
                with lock:
                    if not pending:
                        output_queue.put(None)  # Sentinel to signal done
                        break

    input_thread = threading.Thread(target=input_consumer, daemon=True)
    checker_thread = threading.Thread(target=checker, daemon=True)
    input_thread.start()
    checker_thread.start()

    try:
        while True:
            item = output_queue.get()
            if item is None:  # Sentinel
                break
            yield item
    finally:
        stop_event.set()
        input_thread.join(timeout=1.0)
        checker_thread.join(timeout=1.0)


@register_segment("fileExistsFilter")
@segment()
def FileExistsFilter(items: Any,
                       path_field: Annotated[str, "Field name containing the file path to check"] = "path"):
    """
    Segment that filters out items where the file path doesn't exist.

    Expects input items as dicts with a field containing a file path.
    Only yields items where the file exists on the filesystem.

    This is useful for handling race conditions where files are deleted
    between watchdog detection and processing, or for filtering out
    temporary files that may have been cleaned up.

    Yields items where the specified path field points to an existing file.
    """
    for item in items:
        path = item.get(path_field)
        if path and os.path.exists(path):
            yield item


@register_segment("deleteFile")
@segment()
def DeleteFile(items: Any,
               path_field: Annotated[str, "Field name containing the file path to delete"] = "source"):
    """
    Segment that deletes source files after yielding items.

    Expects input items as dicts with a field containing a file path.
    Yields all items unchanged, but deletes the source file after each item is yielded.

    This is useful for cleaning up source files after they've been successfully
    indexed into the vault. Use with caution as deletion is permanent.

    Silently skips deletion if the file doesn't exist or can't be deleted.
    """
    for item in items:
        yield item
        # Delete the file after yielding to ensure downstream processing can complete
        path = item.get(path_field) if isinstance(item, dict) else None
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except (OSError, PermissionError) as e:
                # Log but don't fail if we can't delete
                import logging
                logging.warning(f"Failed to delete {path}: {e}")


@register_segment("buildVectorDBFromPaths")
@segment()
def build_vector_db_from_paths(items: Any,
                               vault_path: Annotated[str, "Base path for vault storage. Vector DB stored at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"],
                               overwrite: Annotated[bool, "If true, overwrite existing tables and indexes"] = False,
                               delete_after_indexing: Annotated[bool, "If true, delete source files after successfully indexing them"] = False):
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
    ReadFile(field="path") | \
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
        overwrite=overwrite) | \
    setAs(field_list="id:doc_id") | \
    indexWhoosh(
        index_path=whoosh_index_path,
        field_list="content:content,source:path,title:filename",
        overwrite=overwrite,
        commit_seconds=120)

    # Conditionally add file deletion after successful indexing
    if delete_after_indexing:
        base_pipeline = base_pipeline | DeleteFile(path_field="source")

    pipeline = base_pipeline | \
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
        overwrite=False) | \
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
                         delete_after_indexing: Annotated[bool, "If true, delete source files after successfully indexing them"] = False,
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
            delete_after_indexing=delete_after_indexing)
    else:
        pipeline = watcher | \
        Print() | \
        build_vector_db_from_paths(
            vault_path=vault_path,
            overwrite=overwrite,
            delete_after_indexing=delete_after_indexing)

    yield from pipeline()

@register_source("listIntoVectorDB")
@source()
def list_into_vector_db(source_pattern: Annotated[str, "Glob pattern to match files (e.g., '/path/**/*.pdf')"],
                         vault_path: Annotated[str, "Base path for vault storage. Vector DB stored at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"],
                         overwrite: Annotated[bool, "If true, overwrite existing tables and indexes"] = False,
                         delete_after_indexing: Annotated[bool, "If true, delete source files after successfully indexing them"] = False):
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
        delete_after_indexing=delete_after_indexing)
    yield from pipeline([source_pattern])