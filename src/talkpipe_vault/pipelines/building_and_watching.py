from tracemalloc import Filter
from typing import Annotated, Optional, Any
from talkpipe import segment, register_segment, source, register_source
from talkpipe.pipe.io import Print
from talkpipe.data.extraction import listFiles
from talkpipe.pipelines.vector_databases import MakeVectorDatabaseSegment
from talkpipe_vault.watchdog import file_watcher
from talkpipe_vault.docling import DoclingFileToText
from talkpipe.pipe.basic import ToDict, FilterExpression, EvalExpression, setAs
from talkpipe.data.text.chunking_units import splitText, ShingleText


@register_segment("buildVectorDBFromPaths")
@segment()
def build_vector_db_from_paths(items: Any,                         
                               vectordb_path: Annotated[str, "Path to LanceDB database. Supports file paths, 'memory://', or 'tmp://name'"],
                               embedding_model: Annotated[str, "Embedding model to use"],
                               embedding_source: Annotated[str, "Source of text to embed"],
                               overwrite: Annotated[bool, "If true, overwrite existing table"] = False):
    pipeline = \
    FilterExpression(expression="'event' not in item or item['event'] != 'deleted'") | \
    DoclingFileToText(
        field="path",
        set_as="full_content") | \
    FilterExpression(expression="item.get('full_content') and len(item.get('full_content', '').strip()) > 0") | \
    MakeVectorDatabaseSegment(
        path=vectordb_path,
        embedding_model=embedding_model,
        embedding_source=embedding_source,
        embedding_field="full_content",
        table_name="full_documents",
        doc_id_field="path",
        overwrite=overwrite) | \
    ToDict(field_list="path,full_content") | \
    splitText(field="full_content", criteria=500, set_as="chunk") | \
    FilterExpression(expression="item.get('chunk') and len(item.get('chunk', '').strip()) > 0") | \
    ShingleText(field="chunk", shingle_size=3, overlap=1, set_as="shingle_detail", key="path", emit_detail=True) | \
    setAs(field_list="shingle_detail.text:shingle") | \
    EvalExpression(set_as="id", expression="""str(item['shingle_detail']['first_paragraph'])+'-'+str(item['shingle_detail']['last_paragraph'])+'-'+str(item['path'])""") | \
    FilterExpression(expression="item.get('shingle') and len(item.get('shingle', '').strip()) > 0") | \
    MakeVectorDatabaseSegment(
        path=vectordb_path,
        embedding_model=embedding_model,
        embedding_source=embedding_source,
        embedding_field="shingle",
        table_name="shingled_chunks",
        doc_id_field="id",
        overwrite=False)
    yield from pipeline(items)


@register_source("watchIntoVectorDB")
@source()
def watch_into_vector_db(source_path: Annotated[str, "Path to watch"],
                         vectordb_path: Annotated[str, "Path to LanceDB database. Supports file paths, 'memory://', or 'tmp://name'"],
                         embedding_model: Annotated[str, "Embedding model to use"],
                         embedding_source: Annotated[str, "Source of text to embed"],
                         patterns: Annotated[list[str] | None, "List of glob patterns to match"] = None,
                         ignore_patterns: Annotated[list[str] | None, "List of glob patterns to ignore"] = None,
                         ignore_directories: Annotated[bool, "Whether to ignore directory events"] = True,
                         case_sensitive: Annotated[bool, "Whether pattern matching is case-sensitive"] = False,
                         max_events: Annotated[int | None, "Maximum number of events to process"] = None,
                         polling: Annotated[bool, "Use polling-based observer"] = False,
                         ignore_common: Annotated[bool, "Ignore common temp/hidden files"] = True,
                         overwrite: Annotated[bool, "If true, overwrite existing table"] = False,):
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
        vectordb_path=vectordb_path,
        embedding_model=embedding_model,
        embedding_source=embedding_source,
        overwrite=overwrite)
    yield from pipeline()

@register_source("listIntoVectorDB")
@source()
def list_into_vector_db(source_pattern: Annotated[str, "Path to watch"],
                         vectordb_path: Annotated[str, "Path to LanceDB database. Supports file paths, 'memory://', or 'tmp://name'"],
                         embedding_model: Annotated[str, "Embedding model to use"],
                         embedding_source: Annotated[str, "Source of text to embed"],
                         overwrite: Annotated[bool, "If true, overwrite existing table"] = False,):
    pipeline = listFiles(full_path=True, files_only=True) | \
    ToDict(field_list="_:path") | \
    Print() | \
    build_vector_db_from_paths(
        vectordb_path=vectordb_path,
        embedding_model=embedding_model,
        embedding_source=embedding_source,
        overwrite=overwrite)
    yield from pipeline([source_pattern])