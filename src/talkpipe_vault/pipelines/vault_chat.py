"""The ``vaultChat`` segment: retrieval-augmented answers from a vault.

Retrieval (semantic, optionally keyword-boosted and filtered) over one vault
or several, then the RAG prompt, the completion and the sources list.
"""

from typing import Annotated, Any

from talkpipe.chatterlang import register_segment
from talkpipe.llm.chat import LLMPrompt
from talkpipe.pipe.basic import AbstractFieldSegment, ToDict, fillTemplate
from talkpipe.pipe.core import AbstractSegment
from talkpipe.pipelines.basic_rag import AppendRAGSources, ConstructRAGPrompt
from talkpipe.pipelines.vector_databases import SearchVectorDatabaseSegment

from . import retrieval_filter
from .config import (
    DEFAULT_VECTOR_TABLE_NAME,
    RAG_PROMPT_DIRECTIVE,
    RAG_SYSTEM_PROMPT,
    get_chat_model,
    get_chat_source,
    get_retrieval_template,
    get_vector_db_path,
    resolve_embedding_config,
)
from .keyword_search import ExtractSearchKeywords
from .search_results import (
    SEARCH_RESULT_RENAMES,
    MergeSearchResults,
    SearchResultFilter,
)
from .vault_paths import _SearchesVaultPaths, needs_fan_out, resolve_vault_paths
from .vault_search import VaultSearch, VaultTextSearch


@register_segment("vaultChat")
class VaultChat(_SearchesVaultPaths, AbstractFieldSegment):
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
    keyword and semantic hits stay represented after filtering. With several
    vaults or folders, retrieval searches them all (see vaultSearch and
    vaultTextSearch) and the answer cites hits from any of them.

    Emits AI-generated response strings based on retrieved vault context.
    """

    def __init__(
        self,
        vault_path: Annotated[
            str | list[str],
            "Vault path (LanceDB created by makevectordatabase), or a folder "
            "with a folder-mode index; a list searches them together",
        ],
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
        if include_background and not (
            keyword_search
            or result_filter_script
            or needs_fan_out(resolve_vault_paths(vault_path))
        ):
            raise ValueError(
                "include_background requires keyword_search=True, a "
                "result_filter_script, or several vaults"
            )
        self.include_background = include_background
        self.embedding_model, self.embedding_source = resolve_embedding_config(
            embedding_model, embedding_source
        )
        self.chat_model = chat_model if chat_model is not None else get_chat_model()
        self.chat_source = chat_source if chat_source is not None else get_chat_source()
        self.result_limit = limit if limit is not None else 10
        self.keyword_result_limit = (
            keyword_limit if keyword_limit is not None else self.result_limit
        )
        self.keyword_search = keyword_search
        self.result_filter_script = result_filter_script
        self.result_filter_strict = result_filter_strict
        self.vault_path = vault_path
        self._resolve_and_build()

    def _build(self, paths: list[str]) -> None:
        """Retrieval (keyword, semantic, filtered) -> RAG prompt -> completion.

        One pipeline shape for every option: the retrieval stages are added
        as the options ask, and the answer stages are always the same, so an
        answer is worded and cited the same way however it was retrieved.
        The Whoosh index stores LanceDB row ids as doc_ids, so the merge
        deduplicates chunks found by both searches; path/filename are
        renamed to source/title so keyword-only hits are citable.
        """
        # With a filter, each stream over-fetches so it can still fill its
        # result limit after the filter drops hits; the filter truncates its
        # stream back to the limit before the merge (issue #22).
        overfetch = (
            retrieval_filter.FILTER_OVERFETCH_FACTOR if self.result_filter_script else 1
        )
        pipeline: Any = ToDict(field_list="_:query") | fillTemplate(
            template=get_retrieval_template(self.embedding_model),
            set_as="templated_query",
        )
        if self.keyword_search:
            pipeline = (
                pipeline
                | ExtractSearchKeywords(
                    field="query",
                    set_as="_keyword_query",
                    chat_model=self.chat_model,
                    chat_source=self.chat_source,
                )
                | VaultTextSearch(
                    vault_path=paths,
                    limit=self.keyword_result_limit * overfetch,
                    field="_keyword_query",
                    set_as="_keyword_background",
                    multi_emit=False,
                )
            )
            if self.result_filter_script:
                pipeline = pipeline | self._filter(
                    "_keyword_background", self.keyword_result_limit
                )
        pipeline = pipeline | self._vector_stage(paths, self.result_limit * overfetch)
        if self.result_filter_script:
            pipeline = pipeline | self._filter("_background", self.result_limit)
        if self.keyword_search:
            pipeline = pipeline | MergeSearchResults(
                field_list="_background,_keyword_background",
                set_as="_background",
                rename_fields=SEARCH_RESULT_RENAMES,
            )
        self.pipeline = (
            pipeline
            | ConstructRAGPrompt(
                prompt_directive=RAG_PROMPT_DIRECTIVE,
                background_field="_background",
                content_field="query",
                set_as="_ragprompt",
            )
            | LLMPrompt(
                model=self.chat_model,
                source=self.chat_source,
                system_prompt=RAG_SYSTEM_PROMPT,
                field="_ragprompt",
                set_as="_partial_rag_response",
            )
            | AppendRAGSources(
                partial_answer_field="_partial_rag_response",
                set_as="chat_response",
            )
        ).as_function(single_in=True, single_out=True)

    def _filter(self, field: str, limit: int) -> AbstractSegment:
        return SearchResultFilter(
            script=self.result_filter_script or "",
            field=field,
            limit=limit,
            strict=self.result_filter_strict,
            rename_fields=SEARCH_RESULT_RENAMES,
        )

    def _vector_stage(self, paths: list[str], limit: int) -> AbstractSegment:
        """Semantic retrieval of *limit* hits into _background."""
        if needs_fan_out(paths):
            # Folders still embedding are keyword-searched too, so a
            # question can be answered from files not yet embedded.
            return VaultSearch(
                vault_path=paths,
                field="query",
                set_as="_background",
                embedding_model=self.embedding_model,
                embedding_source=self.embedding_source,
                limit=limit,
                keyword_fallback=True,
            )
        return SearchVectorDatabaseSegment(
            path=get_vector_db_path(paths[0]),
            table_name=DEFAULT_VECTOR_TABLE_NAME,
            query_field="templated_query",
            set_as="_background",
            limit=limit,
            embedding_model=self.embedding_model,
            embedding_source=self.embedding_source,
        )

    def process_value(self, value: str) -> Any:
        self._ensure_searchable()
        result = self.pipeline(value)
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
