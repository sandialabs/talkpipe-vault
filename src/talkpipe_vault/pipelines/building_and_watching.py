from typing import Annotated, Any

from talkpipe import register_segment, register_source, segment, source
from talkpipe.data.extraction import ReadFile, listFiles
from talkpipe.data.text.chunking_units import ShingleText, splitText
from talkpipe.pipe.basic import (
    Debounce,
    EvalExpression,
    FilterExpression,
    ToDict,
    fillTemplate,
    setAs,
)
from talkpipe.pipe.io import DeleteFile, FileExistsFilter
from talkpipe.pipelines.vector_databases import MakeVectorDatabaseSegment
from talkpipe.search.whoosh import indexWhoosh

from talkpipe_vault.watchdog import file_watcher

from .config import (
    ensure_supported_vault_layout,
    get_document_template,
    get_shingle_template,
    get_vault_paths,
    get_vector_db_path,
    resolve_embedding_config,
)


def _non_empty_filter(field: str) -> FilterExpression:
    """Return a lambdaFilter segment that keeps items with non-empty field content."""
    return FilterExpression(
        expression=f"item.get('{field}') and len(item.get('{field}', '').strip()) > 0"
    )


@register_segment("buildVectorDBFromPaths")
@segment()
def build_vector_db_from_paths(
    items: Any,
    vault_path: Annotated[
        str,
        "Path to LanceDB directory (makevectordatabase-style). Whoosh full-text index stored at vault_path/fulltext_vault",
    ],
    overwrite: Annotated[
        bool, "If true, overwrite existing tables and indexes"
    ] = False,
    delete_after_reading: Annotated[
        bool, "If true, delete source files after successfully indexing them"
    ] = False,
    batch_size: Annotated[
        int,
        "LanceDB batch size for writes. Smaller values ensure immediate availability but may reduce performance for bulk operations.",
    ] = 1,
    commit_seconds: Annotated[
        float,
        "Whoosh index commit interval in seconds. 0 for immediate commits, higher values batch commits for performance.",
    ] = 0,
    embedding_model: Annotated[
        str | None,
        "Model name for generating embeddings. If None, uses TalkPipe config or default.",
    ] = None,
    embedding_source: Annotated[
        str | None,
        "Source/provider for the embedding model (e.g., 'ollama', 'openai'). If None, uses TalkPipe config or default.",
    ] = None,
):
    """
    Segment that builds a vector database and full-text search index from file paths.

    Expects input items as dicts with the following structure:
        - "path": str - File path to process
        - "event": str (optional) - If present and equals "deleted", item is skipped

    Processes each file through readFile to extract text
    as ExtractionResult objects, then creates embeddings for both full documents and
    shingled chunks. Stores results in two LanceDB tables: 'full_documents' and
    'shingled_chunks'. Also indexes full documents in a Whoosh full-text search index
    for keyword-based retrieval.

    Storage locations derived from vault_path:
        - vault_path: LanceDB vector database
        - vault_path/fulltext_vault: Whoosh full-text search index

    Yields dicts with the following structure:
        - "shingle_id": str - Unique chunk identifier (paragraph range + source path)
        - "shingle": str - Templated text chunk content
        - "source": str - Source file path
        - "title": str - Source file name (from ExtractionResult)
    """
    embedding_model, embedding_source = resolve_embedding_config(
        embedding_model, embedding_source
    )
    ensure_supported_vault_layout(vault_path)
    document_template = get_document_template()
    shingle_template = get_shingle_template()
    vectordb_path = get_vector_db_path(vault_path)
    _, whoosh_index_path = get_vault_paths(vault_path)

    base_pipeline = (
        FilterExpression(expression="'event' not in item or item['event'] != 'deleted'")
        | FileExistsFilter(path_field="path")
        | ReadFile(field="path")
    )
    if delete_after_reading:
        base_pipeline = base_pipeline | DeleteFile(path_field="source")

    full_doc_stage = (
        ToDict(field_list="content,source,id,title")
        | _non_empty_filter("content")
        | fillTemplate(template=document_template, set_as="doc_save_query")
        | MakeVectorDatabaseSegment(
            path=vectordb_path,
            embedding_model=embedding_model,
            embedding_source=embedding_source,
            embedding_field="doc_save_query",
            table_name="full_documents",
            doc_id_field="id",
            overwrite=overwrite,
            fail_on_error=False,
            batch_size=batch_size,
            optimize_on_batch=True,
        )
        | setAs(field_list="id:doc_id")
        | indexWhoosh(
            index_path=whoosh_index_path,
            field_list="content:content,source:path,title:filename",
            overwrite=overwrite,
            commit_seconds=commit_seconds,
        )
        | ToDict(field_list="id,content,title,source")
    )

    shingle_stage = (
        splitText(field="content", criteria=500, set_as="chunk")
        | _non_empty_filter("chunk")
        | ShingleText(
            field="chunk",
            shingle_size=3,
            overlap=1,
            set_as="shingle_detail",
            key="id",
            emit_detail=True,
        )
        | setAs(field_list="shingle_detail.text:shingle")
        | EvalExpression(
            set_as="shingle_id",
            expression="str(item['shingle_detail']['first_paragraph'])+'-'+str(item['shingle_detail']['last_paragraph'])+'-'+str(item['source'])",
        )
        | _non_empty_filter("shingle")
        | ToDict(field_list="shingle_id,shingle,source,title")
        | fillTemplate(template=shingle_template, set_as="shingle")
        | MakeVectorDatabaseSegment(
            path=vectordb_path,
            embedding_model=embedding_model,
            embedding_source=embedding_source,
            embedding_field="shingle",
            table_name="shingled_chunks",
            doc_id_field="shingle_id",
            overwrite=False,
            fail_on_error=False,
            batch_size=batch_size,
            optimize_on_batch=True,
        )
        | ToDict(field_list="shingle_id,shingle,source,title")
    )

    pipeline = base_pipeline | full_doc_stage | shingle_stage
    yield from pipeline(items)


@register_source("watchIntoVectorDB")
@source()
def watch_into_vector_db(
    source_path: Annotated[str, "Path to watch"],
    vault_path: Annotated[
        str,
        "Path to LanceDB directory (makevectordatabase-style). Whoosh full-text index stored at vault_path/fulltext_vault",
    ],
    patterns: Annotated[list[str] | None, "List of glob patterns to match"] = None,
    ignore_patterns: Annotated[
        list[str] | None, "List of glob patterns to ignore"
    ] = None,
    ignore_directories: Annotated[bool, "Whether to ignore directory events"] = True,
    case_sensitive: Annotated[
        bool, "Whether pattern matching is case-sensitive"
    ] = False,
    max_events: Annotated[int | None, "Maximum number of events to process"] = None,
    polling: Annotated[bool, "Use polling-based observer"] = False,
    ignore_common: Annotated[bool, "Ignore common temp/hidden files"] = True,
    overwrite: Annotated[
        bool, "If true, overwrite existing tables and indexes"
    ] = False,
    delete_after_reading: Annotated[
        bool, "If true, delete source files after successfully indexing them"
    ] = False,
    debounce_seconds: Annotated[
        float, "Seconds to wait for file stability before processing (0 to disable)"
    ] = 1.0,
    embedding_model: Annotated[
        str | None,
        "Model name for generating embeddings. If None, uses TalkPipe config or default.",
    ] = None,
    embedding_source: Annotated[
        str | None,
        "Source/provider for the embedding model (e.g., 'ollama', 'openai'). If None, uses TalkPipe config or default.",
    ] = None,
):
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
    embedding_model, embedding_source = resolve_embedding_config(
        embedding_model, embedding_source
    )

    watcher = file_watcher(
        path=source_path,
        patterns=patterns,
        ignore_patterns=ignore_patterns,
        ignore_directories=ignore_directories,
        case_sensitive=case_sensitive,
        max_events=max_events,
        polling=polling,
        ignore_common=ignore_common,
    )

    pipeline = watcher
    if debounce_seconds > 0:
        pipeline = pipeline | Debounce(
            key_field="path", debounce_seconds=debounce_seconds
        )
    pipeline = pipeline | build_vector_db_from_paths(
        vault_path=vault_path,
        overwrite=overwrite,
        delete_after_reading=delete_after_reading,
        batch_size=1,
        commit_seconds=0,
        embedding_model=embedding_model,
        embedding_source=embedding_source,
    )
    yield from pipeline()


@register_source("listIntoVectorDB")
@source()
def list_into_vector_db(
    source_pattern: Annotated[
        str, "Glob pattern to match files (e.g., '/path/**/*.pdf')"
    ],
    vault_path: Annotated[
        str,
        "Path to LanceDB directory (makevectordatabase-style). Whoosh full-text index stored at vault_path/fulltext_vault",
    ],
    overwrite: Annotated[
        bool, "If true, overwrite existing tables and indexes"
    ] = False,
    delete_after_reading: Annotated[
        bool, "If true, delete source files after successfully indexing them"
    ] = False,
    embedding_model: Annotated[
        str | None,
        "Model name for generating embeddings. If None, uses TalkPipe config or default.",
    ] = None,
    embedding_source: Annotated[
        str | None,
        "Source/provider for the embedding model (e.g., 'ollama', 'openai'). If None, uses TalkPipe config or default.",
    ] = None,
):
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
    embedding_model, embedding_source = resolve_embedding_config(
        embedding_model, embedding_source
    )

    pipeline = (
        listFiles(full_path=True, files_only=True)
        | ToDict(field_list="_:path")
        | build_vector_db_from_paths(
            vault_path=vault_path,
            overwrite=overwrite,
            delete_after_reading=delete_after_reading,
            batch_size=1000,
            commit_seconds=120,
            embedding_model=embedding_model,
            embedding_source=embedding_source,
        )
    )
    yield from pipeline([source_pattern])
