import json
import logging
import re
from collections.abc import Iterable, Iterator
from typing import Annotated, Any

from talkpipe import segment
from talkpipe.chatterlang import register_segment
from talkpipe.llm.chat import LLMPrompt
from talkpipe.pipe.basic import (
    AbstractFieldSegment,
    EvalExpression,
    ToDict,
    fillTemplate,
)
from talkpipe.pipe.core import AbstractSegment, is_metadata
from talkpipe.pipe.metadata import Flush
from talkpipe.pipelines.basic_rag import (
    AppendRAGSources,
    ConstructRAGPrompt,
    RAGToText,
)
from talkpipe.pipelines.vector_databases import SearchVectorDatabaseSegment
from talkpipe.search.abstract import SearchResult
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshIndexError, searchWhoosh
from talkpipe.util.config import parse_key_value_str
from talkpipe.util.data_manipulation import assign_property, extract_property

from . import retrieval_filter
from .config import (
    DEFAULT_VECTOR_TABLE_NAME,
    KEYWORD_EXTRACTION_SYSTEM_PROMPT,
    RAG_PROMPT_DIRECTIVE,
    RAG_SYSTEM_PROMPT,
    ensure_supported_vault_layout,
    get_chat_model,
    get_chat_source,
    get_retrieval_template,
    get_vector_db_path,
    get_whoosh_index_path,
    resolve_embedding_config,
)

logger = logging.getLogger(__name__)

# Document-field renames applied when search results are normalized: Whoosh
# hits carry path/filename while the RAG prompt and sources list cite
# source/title. Used by both the merge (issue #11) and the retrieval filter
# (issue #22), so filter scripts see one document shape from either search.
SEARCH_RESULT_RENAMES = "path:source,filename:title"


def normalize_search_result(
    entry: Any, rename_fields: dict[str, str] | None = None
) -> SearchResult | None:
    """Normalize a search hit (SearchResult or dict) to a SearchResult.

    A field in ``rename_fields`` is only renamed when the new name is absent
    from the document. Returns None for entries of any other shape.
    """
    if isinstance(entry, SearchResult):
        score, doc_id = entry.score, entry.doc_id
        document = dict(entry.document or {})
    elif isinstance(entry, dict):
        score = float(entry.get("score", 0.0))
        doc_id = str(entry.get("doc_id", ""))
        document = dict(entry.get("document") or {})
    else:
        return None
    for old, new in (rename_fields or {}).items():
        if old in document and new not in document:
            document[new] = document.pop(old)
    return SearchResult(score=score, doc_id=doc_id, document=document)


def search_result_to_dict(
    entry: Any, rename_fields: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """Normalize a search hit into the plain dict shape filter scripts consume."""
    result = normalize_search_result(entry, rename_fields)
    if result is None:
        return None
    return {
        "doc_id": result.doc_id,
        "score": result.score,
        "document": dict(result.document or {}),
    }


def normalize_document_cell(raw: Any) -> Any:
    """Normalize a LanceDB ``document`` cell (JSON text, dict, or other) for use
    as a dict-like document."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"content": raw}
    if isinstance(raw, dict):
        return raw
    return {"content": str(raw)}


def _has_document_fts_index(table: Any, field_name: str) -> bool:
    """Return True when an FTS index exists for the target field."""
    try:
        indices = table.list_indices()
    except Exception:
        return False

    for index in indices:
        try:
            if str(index.index_type).upper() != "FTS":
                continue
            if field_name in list(index.columns):
                return True
        except Exception:
            logger.debug("Skipping unreadable index entry", exc_info=True)
            continue
    return False


def _run_fts_search(table: Any, query: str, limit: int) -> list[dict[str, Any]]:
    """Run an FTS search using an existing LanceDB FTS index."""
    rows: list[dict[str, Any]] = (
        table.search(query, query_type="fts").limit(limit).to_list()
    )
    return rows


@register_segment("searchLance")
@segment(process_metadata=True)
def searchLance(
    queries: Annotated[Iterable[Any], "Iterator of query strings"],
    path: Annotated[
        str,
        "Path to LanceDB directory containing the target table",
    ],
    table_name: Annotated[str, "Table name to search"] = DEFAULT_VECTOR_TABLE_NAME,
    limit: Annotated[int, "Maximum number of results per query"] = 100,
    all_results_at_once: Annotated[
        bool,
        "If True, emit a list of results per query. Otherwise emit one result at a time.",
    ] = False,
    continue_on_error: Annotated[
        bool, "If True, continue processing when a query fails"
    ] = True,
    field: Annotated[str, "Field to extract query from"] = "_",
    set_as: Annotated[str | None, "Field name to set results on input items"] = None,
) -> Iterator[Any]:
    """
    Search LanceDB documents using an existing LanceDB full-text search index.

    Expects a LanceDB table containing TalkPipe document records where each row can
    be loaded through LanceDBDocumentStore and converted to a dict-like document.
    """
    if set_as is not None and not all_results_at_once:
        raise ValueError("set_as requires all_results_at_once=True")

    doc_store = LanceDBDocumentStore(path=path, table_name=table_name)
    table, _ = doc_store._get_table()
    keyword_search_enabled = _has_document_fts_index(table, "document")

    for item in queries:
        if is_metadata(item) and isinstance(item, Flush):
            continue

        if not keyword_search_enabled:
            if all_results_at_once:
                if set_as:
                    assign_property(item, set_as, [])
                    yield item
                else:
                    yield []
            continue

        query = extract_property(item, field, fail_on_missing=True)
        try:
            rows = _run_fts_search(table, str(query), limit)
            results = [
                {
                    "doc_id": row.get("id", ""),
                    "score": float(row.get("_score", 0.0)),
                    "document": normalize_document_cell(row.get("document", "{}")),
                }
                for row in rows
            ]

            if all_results_at_once:
                if set_as:
                    assign_property(item, set_as, results)
                    yield item
                else:
                    yield results
            else:
                yield from results
        except Exception:
            if not continue_on_error:
                raise


_KEYWORD_SPLIT_RE = re.compile(r"[\n,;]+")
_KEYWORD_PREFIX_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")
_KEYWORD_SANITIZE_RE = re.compile(r"[^\w\s-]+")


def keyword_query_from_llm_output(text: Any, max_keywords: int = 8) -> str:
    """Turn raw LLM output into a Whoosh OR-query of quoted terms.

    The output is split on newlines, commas, and semicolons; list markers and
    punctuation are stripped from each candidate. Candidates that are empty,
    duplicated, or too long to be a keyword (more than four words — usually
    LLM commentary) are dropped. Every surviving term is double-quoted so it
    is parsed as a literal term/phrase rather than Whoosh query syntax.
    Returns "" when nothing usable remains.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for part in _KEYWORD_SPLIT_RE.split(str(text)):
        candidate = _KEYWORD_PREFIX_RE.sub("", part)
        candidate = " ".join(_KEYWORD_SANITIZE_RE.sub(" ", candidate).split())
        if not candidate or not any(ch.isalnum() for ch in candidate):
            continue
        if len(candidate.split()) > 4:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(candidate)
        if len(terms) >= max_keywords:
            break
    return " OR ".join(f'"{term}"' for term in terms)


@register_segment("extractSearchKeywords")
class ExtractSearchKeywords(AbstractFieldSegment):
    """
    Segment that turns a natural-language question into a full-text search query.

    Expects input items containing the question text (either as the full item
    or in a specified field). An LLM is asked for a handful of index-worthy
    keywords and phrases, which are combined into a single OR query suitable
    for a Whoosh full-text index. If the LLM call fails or yields nothing
    usable, the original question text is emitted instead so a downstream
    keyword search still runs.

    Emits the keyword query string (or sets it on the item via set_as).
    """

    def __init__(
        self,
        field: Annotated[
            str | None, "The field to extract. If none, use full item."
        ] = None,
        set_as: Annotated[str | None, "The field to set/append the result as."] = None,
        multi_emit: Annotated[
            bool,
            "Whether this class potentially emits multiple results per item."
            "Should be set by the subclass constructor call or the field_segment decorator, not by the user.",
        ] = False,
        chat_model: Annotated[
            str | None,
            "Model name for keyword extraction. If None, uses TalkPipe config or default.",
        ] = None,
        chat_source: Annotated[
            str | None,
            "Source/provider for the extraction model (e.g., 'ollama', 'openai'). If None, uses TalkPipe config or default.",
        ] = None,
        max_keywords: Annotated[
            int, "Maximum number of keywords to include in the query."
        ] = 8,
    ):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        chat_model = chat_model if chat_model is not None else get_chat_model()
        chat_source = chat_source if chat_source is not None else get_chat_source()
        self.max_keywords = max_keywords
        self._extract = LLMPrompt(
            model=chat_model,
            source=chat_source,
            system_prompt=KEYWORD_EXTRACTION_SYSTEM_PROMPT,
            multi_turn=False,
        ).as_function(single_in=True, single_out=True)

    def process_value(self, value: Any) -> str:
        question = str(value)
        try:
            raw = self._extract(question)
        except Exception:
            logger.warning(
                "Keyword extraction failed; using the raw question as the keyword query",
                exc_info=True,
            )
            return question
        query = keyword_query_from_llm_output(raw, self.max_keywords)
        if not query:
            logger.warning(
                "Keyword extraction produced no usable keywords; "
                "using the raw question as the keyword query"
            )
            return question
        return query


@register_segment("mergeSearchResults")
class MergeSearchResults(AbstractSegment):
    """
    Segment that merges search-result lists from several fields into one list.

    Expects dict-like input items where each named field holds a list of search
    results — either talkpipe SearchResult objects or dicts with doc_id, score,
    and document keys (the shapes produced by vaultSearch and vaultTextSearch).
    Results are normalized to SearchResult, deduplicated (by doc_id when
    present, otherwise by document content), and stored on the item in order:
    everything from the first field, then previously unseen results from each
    later field. A missing field contributes nothing.
    """

    def __init__(
        self,
        field_list: Annotated[
            str, "Comma-separated fields holding search-result lists, merged in order"
        ],
        set_as: Annotated[str, "The field to set the merged list as."],
        rename_fields: Annotated[
            str | None,
            "Document fields to rename during normalization, as 'old:new,old:new'. "
            "A field is only renamed when the new name is absent.",
        ] = None,
        limit: Annotated[
            int | None, "Maximum total number of merged results to keep."
        ] = None,
    ):
        super().__init__()
        self.fields = [f.strip() for f in field_list.split(",") if f.strip()]
        self.set_as = set_as
        self.rename_fields = (
            parse_key_value_str(rename_fields, require_value=True)
            if rename_fields
            else {}
        )
        self.limit = limit

    def _normalize(self, entry: Any) -> SearchResult | None:
        return normalize_search_result(entry, self.rename_fields)

    def transform(self, input_iter: Iterable[Any]) -> Iterator[Any]:
        for item in input_iter:
            merged: list[SearchResult] = []
            seen: set[str] = set()
            for field in self.fields:
                results = extract_property(item, field, fail_on_missing=False)
                for entry in results or []:
                    result = self._normalize(entry)
                    if result is None:
                        continue
                    key = result.doc_id or json.dumps(
                        result.document, sort_keys=True, default=str
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(result)
            if self.limit is not None:
                merged = merged[: self.limit]
            assign_property(item, self.set_as, merged)
            yield item


@register_segment("filterSearchResults")
class SearchResultFilter(AbstractSegment):
    """
    Segment that runs a user-supplied ChatterLang script over search results.

    Expects dict-like input items where a named field holds a list of search
    results (SearchResult objects or dicts with doc_id, score, and document
    keys). Each result is normalized to the plain dict shape
    {"doc_id": ..., "score": ..., "document": {...}} and streamed through the
    script one result per item; the script keeps a result by emitting it,
    filters it by dropping it, and transforms it by modifying it. The script's
    output is normalized back to SearchResult objects (unusable emissions are
    dropped with a warning), optionally truncated, and stored on the item.

    When the script fails at run time, the unfiltered results are used and the
    error is recorded on the item as _filter_error — unless strict is set, in
    which case the failure propagates. Strict exists for redaction-style
    filters, where silently proceeding with unfiltered results would leak the
    very content the filter removes.
    """

    def __init__(
        self,
        script: Annotated[
            str,
            "ChatterLang filter script: a single segment-only pipeline "
            "(no INPUT FROM source, loops, or forks).",
        ],
        field: Annotated[str, "The field holding the search-result list."],
        set_as: Annotated[
            str | None,
            "The field to set the filtered list as. If None, uses field.",
        ] = None,
        limit: Annotated[
            int | None,
            "Maximum number of results to keep after filtering.",
        ] = None,
        strict: Annotated[
            bool,
            "If True, a script failure at run time raises instead of "
            "falling back to the unfiltered results.",
        ] = False,
        rename_fields: Annotated[
            str | None,
            "Document fields to rename during normalization, as 'old:new,"
            "old:new'. A field is only renamed when the new name is absent.",
        ] = None,
    ):
        super().__init__()
        self.field = field
        self.set_as = set_as if set_as is not None else field
        self.limit = limit
        self.strict = strict
        self.rename_fields = (
            parse_key_value_str(rename_fields, require_value=True)
            if rename_fields
            else {}
        )
        # Raises ValueError with a user-readable message on an unusable
        # script, so a broken filter surfaces at pipeline-build time.
        self._compiled = retrieval_filter.compile_script(script)

    def apply(self, results: list[Any]) -> tuple[list[SearchResult], str | None]:
        """Run the script over a result list; return (filtered, error).

        The error is None when the script ran, and the failure message when it
        did not and the unfiltered results were kept (non-strict mode).
        """
        prepared = [
            entry
            for entry in (
                search_result_to_dict(raw, self.rename_fields) for raw in results or []
            )
            if entry is not None
        ]
        try:
            filtered = []
            for emitted in self._compiled(iter(prepared)):
                normalized = normalize_search_result(emitted)
                if normalized is None:
                    logger.warning(
                        "The retrieval filter emitted a %s instead of a "
                        "search result; dropping it",
                        type(emitted).__name__,
                    )
                    continue
                filtered.append(normalized)
            error = None
        except Exception as exc:
            if self.strict:
                raise RuntimeError(
                    f"The vault's retrieval filter failed: {exc}"
                ) from exc
            logger.warning(
                "Retrieval filter failed; using the unfiltered results",
                exc_info=True,
            )
            filtered = [
                entry
                for entry in (
                    normalize_search_result(raw, self.rename_fields)
                    for raw in results or []
                )
                if entry is not None
            ]
            error = str(exc)
        if self.limit is not None:
            filtered = filtered[: self.limit]
        return filtered, error

    def transform(self, input_iter: Iterable[Any]) -> Iterator[Any]:
        for item in input_iter:
            results = extract_property(item, self.field, fail_on_missing=False)
            filtered, error = self.apply(results or [])
            assign_property(item, self.set_as, filtered)
            if error:
                assign_property(item, "_filter_error", error)
            yield item


@register_segment("vaultSearch")
class VaultSearch(AbstractFieldSegment):
    """
    Segment that performs semantic search on a vault's vector database.

    Expects input items containing a search query string (either as the full item
    or in a specified field). The query is templated and used to search the
    'docs' table in LanceDB.

    Emits search results from the vector database containing matching document chunks.
    """

    def __init__(
        self,
        vault_path: Annotated[str, "Path to LanceDB created by makevectordatabase"],
        field: Annotated[
            str | None, "The field to extract. If none, use full item."
        ] = None,
        set_as: Annotated[str | None, "The field to set/append the result as."] = None,
        multi_emit: Annotated[
            bool,
            "Whether this class potentially emits multiple results per item."
            "Should be set by the subclass constructor call or the field_segment decorator, not by the user.",
        ] = False,
        embedding_model: Annotated[
            str | None,
            "Model name for generating embeddings. If None, uses TalkPipe config or default.",
        ] = None,
        embedding_source: Annotated[
            str | None,
            "Source/provider for the embedding model (e.g., 'ollama', 'openai'). If None, uses TalkPipe config or default.",
        ] = None,
        limit: Annotated[
            int | None,
            "Maximum number of results. If None, uses the vector store default.",
        ] = None,
    ):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        embedding_model, embedding_source = resolve_embedding_config(
            embedding_model, embedding_source
        )

        retrieval_template = get_retrieval_template()
        ensure_supported_vault_layout(vault_path)

        self.vault_path = vault_path
        vectordb_path = get_vector_db_path(vault_path)
        limit_kwargs = {} if limit is None else {"limit": limit}
        self.pipeline = (
            ToDict(field_list="_:query")
            | fillTemplate(template=retrieval_template, set_as="templated_query")
            | SearchVectorDatabaseSegment(
                path=vectordb_path,
                table_name=DEFAULT_VECTOR_TABLE_NAME,
                query_field="templated_query",
                embedding_model=embedding_model,
                embedding_source=embedding_source,
                **limit_kwargs,
            )
        ).as_function(single_in=True, single_out=True)

    def process_value(self, value: str) -> Any:
        return self.pipeline(value)


@register_segment("vaultChat")
class VaultChat(AbstractFieldSegment):
    """
    Segment that provides RAG-based conversational AI using vault contents.

    Expects input items containing a user query string (either as the full item
    or in a specified field). The query is used to retrieve relevant context from
    the vault's 'docs' table, which is then used to generate a response. With
    keyword_search enabled, an LLM additionally distills the query into
    full-text index keywords, the vault's Whoosh index is searched with them,
    and those hits are merged into the retrieved context before answering.
    With a result_filter_script, each retrieval stream is over-fetched, run
    through the vault's ChatterLang filter independently, and truncated back
    to its result limit before the streams are merged (issue #22), so both
    keyword and semantic hits stay represented after filtering.

    Emits AI-generated response strings based on retrieved vault context.
    """

    def __init__(
        self,
        vault_path: Annotated[str, "Path to LanceDB created by makevectordatabase"],
        field: Annotated[
            str | None, "The field to extract. If none, use full item."
        ] = None,
        set_as: Annotated[str | None, "The field to set/append the result as."] = None,
        multi_emit: Annotated[
            bool,
            "Whether this class potentially emits multiple results per item."
            "Should be set by the subclass constructor call or the field_segment decorator, not by the user.",
        ] = False,
        embedding_model: Annotated[
            str | None,
            "Model name for generating embeddings. If None, uses TalkPipe config or default.",
        ] = None,
        embedding_source: Annotated[
            str | None,
            "Source/provider for the embedding model (e.g., 'ollama', 'openai'). If None, uses TalkPipe config or default.",
        ] = None,
        chat_model: Annotated[
            str | None,
            "Model name for chat/completion. If None, uses TalkPipe config or default.",
        ] = None,
        chat_source: Annotated[
            str | None,
            "Source/provider for the chat model (e.g., 'ollama', 'openai'). If None, uses TalkPipe config or default.",
        ] = None,
        limit: Annotated[
            int | None,
            "Number of search results to include in the RAG evaluation.",
        ] = None,
        keyword_search: Annotated[
            bool,
            "If True, also search the vault's Whoosh full-text index with "
            "LLM-extracted keywords and merge those hits into the RAG context.",
        ] = False,
        keyword_limit: Annotated[
            int | None,
            "Number of keyword-search results to merge in. If None, uses limit.",
        ] = None,
        include_background: Annotated[
            bool,
            "If True (requires keyword_search or result_filter_script), emit "
            "a dict with 'response', 'background' (the search results the RAG "
            "prompt was built from), 'filter_error' (why the retrieval filter "
            "fell back to unfiltered results, or None), and — with "
            "keyword_search — 'keyword_hits' (how many chunks the keyword "
            "search returned) instead of the answer string.",
        ] = False,
        result_filter_script: Annotated[
            str | None,
            "ChatterLang script filtering/transforming each retrieval stream "
            "before the RAG prompt (a single segment-only pipeline).",
        ] = None,
        result_filter_strict: Annotated[
            bool,
            "If True, a retrieval-filter failure at run time fails the "
            "question instead of answering from unfiltered results.",
        ] = False,
    ):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        if include_background and not (keyword_search or result_filter_script):
            raise ValueError(
                "include_background requires keyword_search=True or a "
                "result_filter_script"
            )
        self.include_background = include_background
        embedding_model, embedding_source = resolve_embedding_config(
            embedding_model, embedding_source
        )
        chat_model = chat_model if chat_model is not None else get_chat_model()
        chat_source = chat_source if chat_source is not None else get_chat_source()

        retrieval_template = get_retrieval_template()
        ensure_supported_vault_layout(vault_path)

        self.vault_path = vault_path
        vectordb_path = get_vector_db_path(vault_path)
        result_limit = limit if limit is not None else 10
        keyword_result_limit = (
            keyword_limit if keyword_limit is not None else result_limit
        )
        # With a filter, each stream over-fetches so it can still fill its
        # result limit after the filter drops hits; the filter truncates its
        # stream back to the limit before the merge (issue #22).
        overfetch = (
            retrieval_filter.FILTER_OVERFETCH_FACTOR if result_filter_script else 1
        )
        if keyword_search:
            # prompt -> keyword-query creation -> keyword search -> add in
            # vector search -> RAG prompt -> completion, per issue #11. The
            # Whoosh index stores LanceDB row ids as doc_ids, so the merge
            # deduplicates chunks found by both searches; path/filename are
            # renamed to source/title so keyword-only hits are citable by the
            # RAG prompt and the appended sources list.
            pipeline = (
                ToDict(field_list="_:query")
                | fillTemplate(template=retrieval_template, set_as="templated_query")
                | ExtractSearchKeywords(
                    field="query",
                    set_as="_keyword_query",
                    chat_model=chat_model,
                    chat_source=chat_source,
                )
                | VaultTextSearch(
                    vault_path=vault_path,
                    limit=keyword_result_limit * overfetch,
                    field="_keyword_query",
                    set_as="_keyword_background",
                    multi_emit=False,
                )
            )
            if result_filter_script:
                pipeline = pipeline | SearchResultFilter(
                    script=result_filter_script,
                    field="_keyword_background",
                    limit=keyword_result_limit,
                    strict=result_filter_strict,
                    rename_fields=SEARCH_RESULT_RENAMES,
                )
            pipeline = pipeline | SearchVectorDatabaseSegment(
                path=vectordb_path,
                table_name=DEFAULT_VECTOR_TABLE_NAME,
                query_field="templated_query",
                set_as="_background",
                limit=result_limit * overfetch,
                embedding_model=embedding_model,
                embedding_source=embedding_source,
            )
            if result_filter_script:
                pipeline = pipeline | SearchResultFilter(
                    script=result_filter_script,
                    field="_background",
                    limit=result_limit,
                    strict=result_filter_strict,
                    rename_fields=SEARCH_RESULT_RENAMES,
                )
            self.pipeline = (
                pipeline
                | MergeSearchResults(
                    field_list="_background,_keyword_background",
                    set_as="_background",
                    rename_fields=SEARCH_RESULT_RENAMES,
                )
                | ConstructRAGPrompt(
                    prompt_directive=RAG_PROMPT_DIRECTIVE,
                    background_field="_background",
                    content_field="query",
                    set_as="_ragprompt",
                )
                | LLMPrompt(
                    model=chat_model,
                    source=chat_source,
                    system_prompt=RAG_SYSTEM_PROMPT,
                    field="_ragprompt",
                    set_as="_partial_rag_response",
                )
                | AppendRAGSources(
                    partial_answer_field="_partial_rag_response",
                    set_as="chat_response",
                )
            ).as_function(single_in=True, single_out=True)
        elif result_filter_script:
            # The RAGToText composite hides retrieval internally, so the
            # filtered plain pipeline uses the decomposed equivalent: vector
            # search into _background, the filter, then the same RAG prompt ->
            # completion -> sources stages the keyword branch uses.
            self.pipeline = (
                ToDict(field_list="_:query")
                | fillTemplate(template=retrieval_template, set_as="templated_query")
                | SearchVectorDatabaseSegment(
                    path=vectordb_path,
                    table_name=DEFAULT_VECTOR_TABLE_NAME,
                    query_field="templated_query",
                    set_as="_background",
                    limit=result_limit * overfetch,
                    embedding_model=embedding_model,
                    embedding_source=embedding_source,
                )
                | SearchResultFilter(
                    script=result_filter_script,
                    field="_background",
                    limit=result_limit,
                    strict=result_filter_strict,
                    rename_fields=SEARCH_RESULT_RENAMES,
                )
                | ConstructRAGPrompt(
                    prompt_directive=RAG_PROMPT_DIRECTIVE,
                    background_field="_background",
                    content_field="query",
                    set_as="_ragprompt",
                )
                | LLMPrompt(
                    model=chat_model,
                    source=chat_source,
                    system_prompt=RAG_SYSTEM_PROMPT,
                    field="_ragprompt",
                    set_as="_partial_rag_response",
                )
                | AppendRAGSources(
                    partial_answer_field="_partial_rag_response",
                    set_as="chat_response",
                )
            ).as_function(single_in=True, single_out=True)
        else:
            self.pipeline = (
                ToDict(field_list="_:query")
                | fillTemplate(template=retrieval_template, set_as="templated_query")
                | RAGToText(
                    path=vectordb_path,
                    content_field="query",
                    embedding_prompt="templated_query",
                    table_name=DEFAULT_VECTOR_TABLE_NAME,
                    set_as="chat_response",
                    embedding_model=embedding_model,
                    embedding_source=embedding_source,
                    completion_model=chat_model,
                    completion_source=chat_source,
                    limit=result_limit,
                    system_prompt=RAG_SYSTEM_PROMPT,
                    prompt_directive=RAG_PROMPT_DIRECTIVE,
                )
                | EvalExpression(field="chat_response", expression="item")
            ).as_function(single_in=True, single_out=True)
        self.keyword_search = keyword_search
        # These branches keep the full working item so callers can see the
        # retrieval and filter outcome, not just the answer.
        self._emits_item = keyword_search or bool(result_filter_script)

    def process_value(self, value: str) -> Any:
        result = self.pipeline(value)
        if not self._emits_item:
            return result
        if self.include_background:
            out = {
                "response": result.get("chat_response", ""),
                "background": result.get("_background") or [],
                "filter_error": result.get("_filter_error"),
            }
            if self.keyword_search:
                out["keyword_hits"] = len(result.get("_keyword_background") or [])
            return out
        return result.get("chat_response", "")


@register_segment("vaultTextSearch")
class VaultTextSearch(AbstractFieldSegment):
    """
    Segment that performs keyword search on a vault's Whoosh index.

    Expects input items containing a search query string (either as the full item
    or in a specified field). The query is matched against document fields in
    the configured Whoosh full-text index.

    Emits search results as dicts containing:
        - "doc_id": str - Document identifier (file path)
        - "score": float - Relevance score
        - "document": dict - Contains "content" field with matched text
    """

    def __init__(
        self,
        vault_path: Annotated[str, "Path to LanceDB created by makevectordatabase"],
        limit: Annotated[int | None, "Maximum number of results to return"] = None,
        field: Annotated[
            str | None, "The field to extract. If none, use full item."
        ] = None,
        set_as: Annotated[str | None, "The field to set/append the result as."] = None,
        multi_emit: Annotated[
            bool,
            "Whether this class potentially emits multiple results per item."
            "Should be set by the subclass constructor call or the field_segment decorator, not by the user.",
        ] = True,
    ):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        ensure_supported_vault_layout(vault_path)
        self.vault_path = vault_path
        whoosh_index_path = get_whoosh_index_path(vault_path)
        self.pipeline = searchWhoosh(
            index_path=whoosh_index_path,
            limit=limit,
            all_results_at_once=False,
        ).as_function(single_in=True, single_out=False)

    def process_value(self, value: str) -> list[dict[str, Any]]:
        results = []
        try:
            for result in self.pipeline(value):
                if isinstance(result, dict):
                    results.append(result)
                    continue
                results.append(
                    {
                        "doc_id": getattr(result, "doc_id", ""),
                        "score": float(getattr(result, "score", 0.0)),
                        "document": getattr(result, "document", {}),
                    }
                )
        except WhooshIndexError:
            return []
        return results
