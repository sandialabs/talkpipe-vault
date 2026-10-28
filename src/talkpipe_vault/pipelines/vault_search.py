"""The ``vaultSearch`` and ``vaultTextSearch`` segments.

Semantic and keyword search over one vault or several; with several (or a
folder index) each index is searched on its own and the hits fused — by
distance where the embedder is shared, by reciprocal rank otherwise — with
each hit labelled by its folder.
"""

import logging
import os
from typing import Annotated, Any

from talkpipe.chatterlang import register_segment
from talkpipe.llm.embedding import LLMEmbed
from talkpipe.pipe.basic import AbstractFieldSegment, ToDict, fillTemplate
from talkpipe.pipelines.vector_databases import SearchVectorDatabaseSegment
from talkpipe.search.abstract import SearchResult
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import WhooshFullTextIndex, WhooshIndexError, searchWhoosh
from talkpipe.util.config import parse_key_value_str

from . import folder_index, vault_metadata
from .config import (
    DEFAULT_VECTOR_TABLE_NAME,
    get_retrieval_template,
    get_vector_db_path,
    get_whoosh_index_path,
    resolve_embedding_config,
)
from .keyword_search import words_query
from .search_results import (
    SEARCH_RESULT_RENAMES,
    normalize_document_cell,
    reciprocal_rank_fusion,
)
from .vault_paths import _index_views, _IndexView, _SearchesVaultPaths, needs_fan_out

logger = logging.getLogger(__name__)


class _FanOutVectorSearch:
    """Semantic search over several indexes, fused into one ranked list.

    Indexes are grouped by the embedder their vectors were made with (the
    one recorded in each index; the given one for an index with no record)
    and the query is embedded once per group. Within a group the hits are
    merged by raw distance, which is comparable there; groups are fused by
    reciprocal rank, since distances from different embedders are not.
    """

    def __init__(
        self,
        paths: list[str],
        embedding_model: str | None,
        embedding_source: str | None,
        limit: int,
    ):
        self.limit = limit
        self.views = _index_views(paths)
        default_model, default_source = resolve_embedding_config(
            embedding_model, embedding_source
        )
        grouped: dict[tuple[str, str], list[_IndexView]] = {}
        for view in self.views:
            recorded = vault_metadata.load_embedding_config(view.path)
            key = (
                (str(recorded["source"]), str(recorded["model"]))
                if recorded
                else (default_source, default_model)
            )
            grouped.setdefault(key, []).append(view)
        # Embedders are made on first use, not here: making one can load or
        # download its model, which would hold up whatever builds this search
        # (opening a folder in the web interface) when the indexer, already
        # running in the background, needs the same model anyway.
        self.groups = [
            (key, get_retrieval_template(key[1]), views)
            for key, views in grouped.items()
        ]
        self._embedders: dict[tuple[str, str], Any] = {}
        self.stores = {
            view.path: LanceDBDocumentStore(
                path=get_vector_db_path(view.path),
                table_name=DEFAULT_VECTOR_TABLE_NAME,
            )
            for view in self.views
        }

    def __call__(self, query: str) -> list[SearchResult]:
        results, failed = self.search_reachable(query)
        if failed:
            raise failed[0][1]
        return results

    def search_reachable(
        self, query: str
    ) -> tuple[list[SearchResult], list[tuple[_IndexView, Exception]]]:
        """Search with every embedder that answers; also return the indexes
        whose embedder failed, with the error, so the caller can fall back
        for just those rather than for every folder."""
        lists: list[list[SearchResult]] = []
        failed: list[tuple[_IndexView, Exception]] = []
        for key, template, group_views in self.groups:
            # An index with no vectors yet has nothing to compare with, and
            # embedding the query for it could wait on the model's first
            # download; with keyword_fallback its keywords answer for it.
            views = [v for v in group_views if folder_index.vectors_started(v.path)]
            if not views:
                continue
            try:
                vector = self._embedder(key).execute_one(
                    template.replace("{query}", str(query))
                )
            except Exception as exc:
                failed.extend((view, exc) for view in views)
                continue
            hits: list[tuple[float, SearchResult]] = []
            for view in views:
                hits.extend(self._search(view, vector))
            hits.sort(key=lambda hit: hit[0])
            lists.append([result for _, result in hits[: self.limit]])
        if len(lists) <= 1:
            return (lists[0] if lists else []), failed
        return reciprocal_rank_fusion(lists, limit=self.limit), failed

    def _embedder(self, key: tuple[str, str]) -> Any:
        if key not in self._embedders:
            source, model = key
            self._embedders[key] = LLMEmbed(
                model=model, source=source, field=None
            ).embedder
        return self._embedders[key]

    def _search(
        self, view: _IndexView, vector: Any
    ) -> list[tuple[float, SearchResult]]:
        try:
            table, _ = self.stores[view.path]._get_table()
            rows = table.search(list(vector)).limit(self.limit).to_list()
        except ValueError:
            return []  # no vectors yet
        except Exception:
            logger.warning("Vector search of %s failed", view.path, exc_info=True)
            return []
        out = []
        for row in rows:
            # Raw distance, not vector_search's score: that clips at zero,
            # which would leave nothing to merge on for some embedders.
            distance = float(row.get("_distance", 0.0))
            out.append(
                (
                    distance,
                    SearchResult(
                        score=max(0.0, 1.0 - distance),
                        doc_id=str(row.get("id", "")),
                        document=view.tag(
                            normalize_document_cell(row.get("document", "{}"))
                        ),
                    ),
                )
            )
        return out


class _FanOutTextSearch:
    """Keyword search over several Whoosh indexes, fused by reciprocal rank.

    Emits the dict shape VaultTextSearch does ({"doc_id", "score",
    "document"}). An index without a full-text index contributes nothing.
    """

    def __init__(self, views: list[_IndexView], limit: int | None):
        self.views = views
        self.limit = limit

    def __call__(self, query: str) -> list[dict[str, Any]]:
        lists: list[list[dict[str, Any]]] = []
        for view in self.views:
            whoosh_path = get_whoosh_index_path(view.path)
            if not os.path.isdir(whoosh_path):
                continue
            try:
                with WhooshFullTextIndex(whoosh_path) as ix:
                    # None means uncapped at runtime, as in VaultTextSearch.
                    hits = ix.text_search(query, limit=self.limit)  # type: ignore[arg-type]  # None = uncapped
            except Exception:
                logger.warning("Keyword search of %s failed", view.path, exc_info=True)
                continue
            lists.append(
                [
                    {
                        "doc_id": hit.doc_id,
                        "score": float(hit.score),
                        "document": view.tag(hit.document),
                    }
                    for hit in hits
                ]
            )
        if len(lists) == 1:
            return lists[0]
        return [
            {"doc_id": r.doc_id, "score": r.score, "document": dict(r.document or {})}
            for r in reciprocal_rank_fusion(lists, limit=self.limit)
        ]


@register_segment("vaultSearch")
class VaultSearch(_SearchesVaultPaths, AbstractFieldSegment):
    """
    Segment that performs semantic search on a vault's vector database.

    Expects input items containing a search query string (either as the full item
    or in a specified field). The query is templated and used to search the
    'docs' table in LanceDB.

    With several vaults or folders (or a single folder index), each is
    searched with the embedder its vectors were made with and the hits are
    fused into one ranked list; each hit's document gets a "folder" label.
    With keyword_fallback, folder indexes still embedding are also searched
    by keyword and those hits fused in, so files not yet embedded can be
    found.

    Emits search results from the vector database containing matching document chunks.
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
        limit: Annotated[
            int | None,
            "Maximum number of results. If None, uses the vector store default.",
        ] = None,
        keyword_fallback: Annotated[
            bool,
            "If True, also keyword-search folder indexes whose files are not "
            "all embedded yet and fuse those hits in.",
        ] = False,
    ):
        super().__init__(field=field, set_as=set_as, multi_emit=multi_emit)
        self.embedding_model, self.embedding_source = resolve_embedding_config(
            embedding_model, embedding_source
        )
        self.limit = limit
        self.keyword_fallback = keyword_fallback
        self.vault_path = vault_path
        self._resolve_and_build()

    def _build(self, paths: list[str]) -> None:
        if needs_fan_out(paths):
            self.pipeline = self._fan_out(paths)
            return
        retrieval_template = get_retrieval_template(self.embedding_model)
        limit_kwargs: dict[str, Any] = (
            {} if self.limit is None else {"limit": self.limit}
        )
        self.pipeline = (
            ToDict(field_list="_:query")
            | fillTemplate(template=retrieval_template, set_as="templated_query")
            | SearchVectorDatabaseSegment(
                path=get_vector_db_path(paths[0]),
                table_name=DEFAULT_VECTOR_TABLE_NAME,
                query_field="templated_query",
                embedding_model=self.embedding_model,
                embedding_source=self.embedding_source,
                **limit_kwargs,
            )
        ).as_function(single_in=True, single_out=True)

    def _fan_out(self, paths: list[str]) -> Any:
        result_limit = self.limit if self.limit is not None else 10
        keyword_fallback = self.keyword_fallback
        search = _FanOutVectorSearch(
            paths, self.embedding_model, self.embedding_source, result_limit
        )

        def run(value: Any) -> list[SearchResult]:
            if not keyword_fallback:
                return search(str(value))
            results, failed = search.search_reachable(str(value))
            for view, exc in failed:
                # Its embedder is unreachable: answer for that folder from
                # keywords alone rather than not at all.
                logger.warning(
                    "Semantic search of %s failed (%s); using keyword search for it",
                    view.label,
                    exc,
                )
            failed_paths = {view.path for view, _ in failed}
            # Re-checked per query: the index finishes embedding over time.
            unfinished = [
                view
                for view in search.views
                if view.path in failed_paths
                or not folder_index.vectors_complete(view.path)
            ]
            if not unfinished:
                return results
            keyword_hits = _FanOutTextSearch(unfinished, result_limit)(
                words_query(str(value))
            )
            return reciprocal_rank_fusion(
                [results, keyword_hits],
                limit=result_limit,
                rename_fields=parse_key_value_str(SEARCH_RESULT_RENAMES),
            )

        return run

    def process_value(self, value: str) -> Any:
        self._ensure_searchable()
        return self.pipeline(value)


@register_segment("vaultTextSearch")
class VaultTextSearch(_SearchesVaultPaths, AbstractFieldSegment):
    """
    Segment that performs keyword search on a vault's Whoosh index.

    Expects input items containing a search query string (either as the full item
    or in a specified field). The query is matched against document fields in
    the configured Whoosh full-text index; with several vaults or folders,
    each index is searched and the hits fused by reciprocal rank, each
    document labelled with its "folder".

    Emits search results as dicts containing:
        - "doc_id": str - Document identifier (file path)
        - "score": float - Relevance score
        - "document": dict - Contains "content" field with matched text
    """

    def __init__(
        self,
        vault_path: Annotated[
            str | list[str],
            "Vault path (LanceDB created by makevectordatabase), or a folder "
            "with a folder-mode index; a list searches them together",
        ],
        limit: Annotated[
            int | None, "Maximum number of results to return. If None, no cap."
        ] = None,
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
        self.limit = limit
        self.vault_path = vault_path
        self._resolve_and_build()

    def _build(self, paths: list[str]) -> None:
        self._fan_out = (
            _FanOutTextSearch(_index_views(paths), self.limit)
            if needs_fan_out(paths)
            else None
        )
        self.pipeline = searchWhoosh(
            index_path=get_whoosh_index_path(paths[0]),
            # searchWhoosh declares limit as int, but it is passed straight to
            # whoosh's searcher.search(), where None means "all results" -- the
            # behavior this segment relies on to avoid a UI-facing result cap.
            limit=self.limit,  # type: ignore[arg-type]  # None = uncapped at runtime
            all_results_at_once=False,
        ).as_function(single_in=True, single_out=False)

    def process_value(self, value: str) -> list[dict[str, Any]]:
        self._ensure_searchable()
        if self._fan_out is not None:
            return self._fan_out(value)
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
