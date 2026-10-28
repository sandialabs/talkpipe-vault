"""Search results: one shape, and the operations on lists of them.

Semantic and keyword search return hits of different shapes (talkpipe
``SearchResult`` objects, or dicts with ``doc_id``, ``score`` and
``document``); everything here normalizes them to ``SearchResult`` and
works on lists of those, with no knowledge of any index: renames, reciprocal
rank fusion, the ``mergeSearchResults`` and ``filterSearchResults`` segments.
"""

import json
import logging
from collections.abc import Iterable, Iterator
from typing import Annotated, Any

from talkpipe.chatterlang import register_segment
from talkpipe.pipe.core import AbstractSegment
from talkpipe.search.abstract import SearchResult
from talkpipe.util.config import parse_key_value_str
from talkpipe.util.data_manipulation import assign_property, extract_property

from . import retrieval_filter

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
