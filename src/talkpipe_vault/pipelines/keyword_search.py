"""Keyword (full-text) search building blocks.

The ``searchLance`` segment over a LanceDB full-text index, the
``extractSearchKeywords`` segment that has an LLM distill a question into
a Whoosh OR-query, and the plain helpers that turn text into such a query.
Searching one or more vaults' Whoosh indexes is ``vaultTextSearch`` in
``vault_search``.
"""

import logging
import re
from collections.abc import Iterable, Iterator
from typing import Annotated, Any

from talkpipe import segment
from talkpipe.chatterlang import register_segment
from talkpipe.llm.chat import LLMPrompt
from talkpipe.pipe.basic import AbstractFieldSegment
from talkpipe.pipe.core import is_metadata
from talkpipe.pipe.metadata import Flush
from talkpipe.search.lancedb import LanceDBDocumentStore
from talkpipe.util.data_manipulation import assign_property, extract_property

from .config import (
    DEFAULT_VECTOR_TABLE_NAME,
    KEYWORD_EXTRACTION_SYSTEM_PROMPT,
    get_chat_model,
    get_chat_source,
)
from .search_results import normalize_document_cell

logger = logging.getLogger(__name__)


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


def words_query(text: str, max_words: int = 12) -> str:
    """A Whoosh OR-query of the words of *text* (three letters or more).

    For keyword retrieval with no model to distill the question: Whoosh's
    parser joins bare words with AND, which a whole question rarely matches.
    """
    words = [word for word in re.findall(r"\w+", str(text)) if len(word) > 2]
    return keyword_query_from_llm_output("\n".join(words), max_keywords=max_words)


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
