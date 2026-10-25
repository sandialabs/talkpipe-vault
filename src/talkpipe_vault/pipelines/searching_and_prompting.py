import json
import logging
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from talkpipe import segment
from talkpipe.chatterlang import register_segment
from talkpipe.llm.chat import LLMPrompt
from talkpipe.llm.embedding import LLMEmbed
from talkpipe.pipe.basic import (
    AbstractFieldSegment,
    ToDict,
    fillTemplate,
)
from talkpipe.pipe.core import AbstractSegment, is_metadata
from talkpipe.pipe.metadata import Flush
from talkpipe.pipelines.basic_rag import (
    AppendRAGSources,
    ConstructRAGPrompt,
)
from talkpipe.pipelines.vector_databases import SearchVectorDatabaseSegment
from talkpipe.search.abstract import SearchResult
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.search.whoosh import (
    WhooshFullTextIndex,
    WhooshIndexError,
    searchWhoosh,
)
from talkpipe.util.config import parse_key_value_str
from talkpipe.util.data_manipulation import assign_property, extract_property

from . import folder_index, retrieval_filter, vault_metadata
from .config import (
    DEFAULT_VECTOR_TABLE_NAME,
    KEYWORD_EXTRACTION_SYSTEM_PROMPT,
    RAG_PROMPT_DIRECTIVE,
    RAG_SYSTEM_PROMPT,
    ensure_supported_vault_layout,
    existing_non_vault_entries,
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
    later field. With strategy "rrf" the lists are instead fused by
    reciprocal rank (a result ranked high in several lists comes first),
    which suits lists whose scores are not comparable. A missing field
    contributes nothing.
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
        strategy: Annotated[
            str, "'concat' (lists in order) or 'rrf' (reciprocal rank fusion)"
        ] = "concat",
    ):
        super().__init__()
        if strategy not in ("concat", "rrf"):
            raise ValueError(f"Unknown merge strategy {strategy!r}")
        self.strategy = strategy
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
            if self.strategy == "rrf":
                lists = [
                    extract_property(item, field, fail_on_missing=False) or []
                    for field in self.fields
                ]
                assign_property(
                    item,
                    self.set_as,
                    reciprocal_rank_fusion(
                        lists, limit=self.limit, rename_fields=self.rename_fields
                    ),
                )
                yield item
                continue
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


# Reciprocal rank fusion's damping constant; 60 is the value from the
# original paper and what most hybrid-search systems use.
RRF_K = 60


def reciprocal_rank_fusion(
    result_lists: Iterable[Iterable[Any]],
    *,
    limit: int | None = None,
    rename_fields: dict[str, str] | None = None,
    k: int = RRF_K,
) -> list[SearchResult]:
    """Fuse ranked result lists by reciprocal rank.

    Each result scores the sum of 1 / (k + rank) over the lists it appears
    in, so ranks rather than scores decide: BM25 scores from different
    indexes, or distances from different embedders, cannot be compared,
    but positions can. Results are deduplicated by doc_id (else content);
    each keeps its own score and the first copy seen. Ties keep the order of
    first appearance.
    """
    scores: dict[str, float] = {}
    first: dict[str, SearchResult] = {}
    order: list[str] = []
    for results in result_lists:
        for rank, entry in enumerate(results or []):
            result = normalize_search_result(entry, rename_fields)
            if result is None:
                continue
            key = result.doc_id or json.dumps(
                result.document, sort_keys=True, default=str
            )
            if key not in first:
                first[key] = result
                order.append(key)
                scores[key] = 0.0
            scores[key] += 1.0 / (k + rank + 1)
    order.sort(key=lambda key: -scores[key])
    merged = [first[key] for key in order]
    return merged if limit is None else merged[:limit]


def resolve_vault_paths(vault_path: str | Iterable[str]) -> list[str]:
    """The index paths a segment's vault_path names.

    One path or several. A folder that has a folder-mode index (its own
    ``.tpvault``, or one kept in app data) stands for that index, so a
    segment can be pointed at the documents folder itself; any other path
    is a vault and is used as it is.
    """
    paths = [vault_path] if isinstance(vault_path, str) else list(vault_path)
    resolved: list[str] = []
    for raw in paths:
        path = os.path.expanduser(str(raw))
        index = None
        if os.path.isdir(path) and folder_index.index_folder_root(path) is None:
            index = folder_index.find_index_path(path)
        resolved.append(str(index) if index is not None else path)
    if not resolved:
        raise ValueError("vault_path names no vault")
    return resolved


def check_searchable(paths: list[str]) -> None:
    """Refuse a search of a path that does not exist, or of a folder of
    documents that has no index yet, with a ValueError that says so.

    Searching either would otherwise create folders (an empty vault at a
    mistyped path, a full-text index inside the documents folder) and
    quietly find nothing. Checked when a search runs, not when the segment
    is built, so an interface can build its pipelines for a vault before
    the vault is indexed or while it reports one that went missing.
    """
    for path in paths:
        if not os.path.isdir(path):
            what = "is not a folder" if os.path.exists(path) else "does not exist"
            raise ValueError(f"Cannot search {path!r}: it {what}. Check the path.")
        if (
            folder_index.index_folder_root(path) is None
            and existing_non_vault_entries(Path(path)) > 0
        ):
            raise ValueError(
                f"Cannot search {path!r}: it holds documents but no index yet. "
                "Index a folder with indexFolder (once) or watchFolders (kept "
                "up to date), or open it in vault-server or vault-tui; build a "
                "named vault with makevectordatabase or the Vaults & Documents "
                "page."
            )


class _SearchesVaultPaths:
    """Shared by the segments that search one or more vaults or folders.

    A folder stands for its index once it has one, and a segment's search
    is built on the indexes found when the segment is made. One made before
    a folder was indexed (while watchFolders, say, is still starting) would
    otherwise refuse that folder on every query even after it is indexed, so
    before each query the paths are resolved again and, when the indexes
    behind ``vault_path`` changed, ``_build`` is run again for the new ones.
    """

    vault_path: str | list[str]
    _paths: list[str]

    def _build(self, paths: list[str]) -> None:
        """Build the search for *paths*; set only what depends on them."""
        raise NotImplementedError

    def _resolve_and_build(self) -> None:
        paths = resolve_vault_paths(self.vault_path)
        for path in paths:
            ensure_supported_vault_layout(path)
        self._paths = paths
        self._build(paths)

    def _ensure_searchable(self) -> None:
        """Rebuild for indexes that appeared or moved, then refuse bad paths."""
        if resolve_vault_paths(self.vault_path) != self._paths:
            self._resolve_and_build()
        check_searchable(self._paths)


def needs_fan_out(paths: list[str]) -> bool:
    """True unless *paths* is one named vault, which keeps its own pipeline.

    Several indexes are searched one by one and fused; a folder index goes
    the same way even alone, so its results carry the folder and have their
    paths rebased when the folder moved.
    """
    return len(paths) > 1 or folder_index.index_folder_root(paths[0]) is not None


@dataclass(frozen=True)
class _IndexView:
    """One index in a fan-out search: where it is and how to label its hits.

    ``recorded_root`` is the folder path its chunk sources were stored under
    and ``current_root`` where the folder is now; they differ when the
    folder was moved or is mounted elsewhere (host versus container), and
    sources are rebased from one to the other.
    """

    path: str
    label: str
    current_root: str = ""
    recorded_root: str = ""

    def tag(self, document: Any) -> dict[str, Any]:
        doc = dict(document or {})
        doc["folder"] = self.label
        old, new = self.recorded_root, self.current_root
        if old and new and old != new:
            for key in ("source", "path"):
                value = doc.get(key)
                if isinstance(value, str) and value.startswith(old + os.sep):
                    doc[key] = new + value[len(old) :]
        return doc


def _index_views(paths: list[str]) -> list[_IndexView]:
    """Views for the indexes, labelled by folder (or vault) name.

    Two folders with the same name are told apart by their parent's name.
    """
    entries: list[tuple[str, Path, str, str]] = []
    for path in paths:
        root = folder_index.index_folder_root(path)
        if root is None:
            entries.append((path, Path(path), "", ""))
            continue
        recorded = folder_index.read_index_status(path).get("recorded_root")
        entries.append((path, root, str(root), str(recorded or root)))
    names = [where.name or str(where) for _, where, _, _ in entries]
    views = []
    for (path, where, current, recorded), name in zip(entries, names, strict=True):
        label = name
        if names.count(name) > 1 and where.parent.name:
            label = f"{name} ({where.parent.name})"
        views.append(_IndexView(path, label, current, recorded))
    return views


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


def words_query(text: str, max_words: int = 12) -> str:
    """A Whoosh OR-query of the words of *text* (three letters or more).

    For keyword retrieval with no model to distill the question: Whoosh's
    parser joins bare words with AND, which a whole question rarely matches.
    """
    words = [word for word in re.findall(r"\w+", str(text)) if len(word) > 2]
    return keyword_query_from_llm_output("\n".join(words), max_keywords=max_words)


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
