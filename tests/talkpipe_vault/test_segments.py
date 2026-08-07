"""Unit tests for the keyword-augmented retrieval segments.

These tests exercise keyword-query parsing and search-result merging without a
vault, an embedding model, or a reachable LLM (the eliza source stands in for
the LLM where one is structurally required), so they run in every environment.
"""

import pytest
from talkpipe import compile
from talkpipe.search.abstract import SearchResult

from talkpipe_vault.pipelines.searching_and_prompting import (
    SEARCH_RESULT_RENAMES,
    ExtractSearchKeywords,
    MergeSearchResults,
    SearchResultFilter,
    keyword_query_from_llm_output,
)

# Compiles, but every item it touches raises at run time.
FAILING_SCRIPT = '| lambdaFilter[expression="1/0"]'
DROP_DRAFTS = (
    "| lambdaFilter[expression=\"'draft' not in document.get('content', '')\"]"
)
# The same filter written without an expression (issue #27).
DROP_DRAFTS_CONTAINMENT = '| isNotIn[field="document.content", value="draft"]'


def _eliza_extractor() -> ExtractSearchKeywords:
    return ExtractSearchKeywords(chat_source="eliza", chat_model="eliza")


class TestKeywordQueryFromLlmOutput:
    def test_terms_and_phrases_become_quoted_or_query(self):
        query = keyword_query_from_llm_output("python, web framework\ndeployment")
        assert query == '"python" OR "web framework" OR "deployment"'

    def test_list_markers_and_punctuation_are_stripped(self):
        query = keyword_query_from_llm_output('- "python"\n* fastapi!\n1. async/await')
        assert query == '"python" OR "fastapi" OR "async await"'

    def test_duplicates_are_dropped_case_insensitively(self):
        query = keyword_query_from_llm_output("Python\npython\nPYTHON")
        assert query == '"Python"'

    def test_long_commentary_lines_are_dropped(self):
        query = keyword_query_from_llm_output(
            "Here are some keywords that could help with your search\npython"
        )
        assert query == '"python"'

    def test_max_keywords_caps_the_query(self):
        query = keyword_query_from_llm_output("a\nb\nc\nd", max_keywords=2)
        assert query == '"a" OR "b"'

    def test_unusable_output_yields_empty_string(self):
        assert keyword_query_from_llm_output("???\n---\n  ") == ""


class TestExtractSearchKeywords:
    def test_segment_is_registered(self):
        script = compile(
            "| extractSearchKeywords[chat_source='eliza', chat_model='eliza']"
        )
        assert script is not None

    def test_llm_output_is_parsed_into_a_query(self):
        segment = _eliza_extractor()
        segment._extract = lambda question: "python\nweb framework"

        result = segment.process_value("How do I deploy?")

        assert result == '"python" OR "web framework"'

    def test_llm_failure_falls_back_to_the_raw_question(self):
        segment = _eliza_extractor()

        def boom(question):
            raise RuntimeError("model unavailable")

        segment._extract = boom

        assert segment.process_value("How do I deploy?") == "How do I deploy?"

    def test_unusable_llm_output_falls_back_to_the_raw_question(self):
        segment = _eliza_extractor()
        segment._extract = lambda question: "???"

        assert segment.process_value("How do I deploy?") == "How do I deploy?"


class TestMergeSearchResults:
    def test_segment_is_registered(self):
        script = compile("| mergeSearchResults[field_list='a,b', set_as='merged']")
        assert script is not None

    def test_merges_normalizes_and_deduplicates(self):
        segment = MergeSearchResults(
            field_list="vector,keyword",
            set_as="merged",
            rename_fields="path:source,filename:title",
        )
        item = {
            "vector": [
                SearchResult(
                    score=0.9,
                    doc_id="row-1",
                    document={"content": "vector chunk", "source": "/docs/a.txt"},
                )
            ],
            "keyword": [
                # Same chunk found by both searches: dropped as a duplicate.
                {
                    "doc_id": "row-1",
                    "score": 4.2,
                    "document": {"content": "vector chunk"},
                },
                {
                    "doc_id": "row-2",
                    "score": 3.1,
                    "document": {
                        "content": "keyword chunk",
                        "path": "/docs/b.txt",
                        "filename": "b.txt",
                    },
                },
            ],
        }

        (result,) = list(segment([item]))
        merged = result["merged"]

        assert [r.doc_id for r in merged] == ["row-1", "row-2"]
        assert all(isinstance(r, SearchResult) for r in merged)
        # Whoosh-style fields are renamed so keyword hits are citable.
        assert merged[1].document["source"] == "/docs/b.txt"
        assert merged[1].document["title"] == "b.txt"
        assert "path" not in merged[1].document

    def test_missing_fields_and_limit(self):
        segment = MergeSearchResults(
            field_list="vector,keyword", set_as="merged", limit=1
        )
        item = {
            "vector": [
                SearchResult(score=0.9, doc_id="row-1", document={"content": "one"}),
                SearchResult(score=0.8, doc_id="row-2", document={"content": "two"}),
            ]
        }

        (result,) = list(segment([item]))

        assert [r.doc_id for r in result["merged"]] == ["row-1"]

    def test_results_without_doc_id_deduplicate_by_content(self):
        segment = MergeSearchResults(field_list="a,b", set_as="merged")
        item = {
            "a": [{"doc_id": "", "score": 1.0, "document": {"content": "same"}}],
            "b": [{"doc_id": "", "score": 2.0, "document": {"content": "same"}}],
        }

        (result,) = list(segment([item]))

        assert len(result["merged"]) == 1


class TestSearchResultFilter:
    """The per-vault retrieval filter that runs a user script over results."""

    @staticmethod
    def _results():
        return [
            SearchResult(
                score=0.9,
                doc_id="row-1",
                document={
                    "content": "keep this",
                    "path": "/docs/a.txt",
                    "filename": "a.txt",
                },
            ),
            {
                "doc_id": "row-2",
                "score": 0.2,
                "document": {"content": "draft notes"},
            },
        ]

    def test_segment_is_registered(self):
        script = compile(
            "| filterSearchResults[script='| lambdaFilter[expression=\"True\"]', "
            "field='_background']"
        )
        assert script is not None

    def test_unusable_script_fails_at_build_time(self):
        with pytest.raises(ValueError, match="noSuchSegmentExists"):
            SearchResultFilter(script="| noSuchSegmentExists", field="_background")

    def test_drops_filtered_results_and_normalizes_the_rest(self):
        segment = SearchResultFilter(
            script=DROP_DRAFTS,
            field="_background",
            rename_fields=SEARCH_RESULT_RENAMES,
        )

        (result,) = list(segment([{"_background": self._results()}]))
        kept = result["_background"]

        assert [r.doc_id for r in kept] == ["row-1"]
        assert all(isinstance(r, SearchResult) for r in kept)
        # Whoosh-style fields are renamed before the script sees them, so one
        # script works for keyword and semantic hits alike.
        assert kept[0].document["source"] == "/docs/a.txt"
        assert kept[0].document["title"] == "a.txt"
        assert "path" not in kept[0].document

    def test_containment_script_filters_without_an_expression(self):
        """isNotIn is the expression-free form of DROP_DRAFTS (issue #27)."""
        segment = SearchResultFilter(
            script=DROP_DRAFTS_CONTAINMENT,
            field="_background",
            rename_fields=SEARCH_RESULT_RENAMES,
        )

        (result,) = list(segment([{"_background": self._results()}]))
        kept = result["_background"]

        assert [r.doc_id for r in kept] == ["row-1"]
        assert all(isinstance(r, SearchResult) for r in kept)

    def test_containment_script_reads_renamed_document_fields(self):
        """Renames land before the script, so document.source is filterable."""
        segment = SearchResultFilter(
            script='| isIn[field="document.source", value="/docs/"]',
            field="_background",
            rename_fields=SEARCH_RESULT_RENAMES,
        )
        results = [self._results()[0]]

        (result,) = list(segment([{"_background": results}]))

        assert [r.doc_id for r in result["_background"]] == ["row-1"]

    def test_transforms_are_carried_through(self):
        segment = SearchResultFilter(
            script="| lambda[expression=\"{**item, 'score': 1.0}\"]",
            field="_background",
        )

        (result,) = list(segment([{"_background": self._results()}]))

        assert [r.score for r in result["_background"]] == [1.0, 1.0]

    def test_limit_truncates_after_filtering(self):
        segment = SearchResultFilter(
            script='| lambdaFilter[expression="True"]', field="_background", limit=1
        )

        (result,) = list(segment([{"_background": self._results()}]))

        assert [r.doc_id for r in result["_background"]] == ["row-1"]

    def test_set_as_writes_to_another_field(self):
        segment = SearchResultFilter(
            script=DROP_DRAFTS, field="_background", set_as="_filtered"
        )

        (result,) = list(segment([{"_background": self._results()}]))

        assert len(result["_background"]) == 2
        assert [r.doc_id for r in result["_filtered"]] == ["row-1"]

    def test_emissions_that_are_not_results_are_dropped(self):
        segment = SearchResultFilter(
            script='| lambda[expression="42"]', field="_background"
        )

        (result,) = list(segment([{"_background": self._results()}]))

        assert result["_background"] == []

    def test_runtime_failure_falls_back_to_unfiltered_results(self):
        """A broken filter must not take down Ask when it only prunes noise."""
        segment = SearchResultFilter(script=FAILING_SCRIPT, field="_background")

        (result,) = list(segment([{"_background": self._results()}]))

        assert [r.doc_id for r in result["_background"]] == ["row-1", "row-2"]
        assert "division by zero" in result["_filter_error"]

    def test_strict_mode_raises_instead_of_leaking_unfiltered_results(self):
        """Redaction filters must fail closed, not answer from raw results."""
        segment = SearchResultFilter(
            script=FAILING_SCRIPT, field="_background", strict=True
        )

        with pytest.raises(RuntimeError, match="retrieval filter failed"):
            list(segment([{"_background": self._results()}]))

    def test_missing_field_yields_no_results_without_failing(self):
        segment = SearchResultFilter(script=DROP_DRAFTS, field="_background")

        (result,) = list(segment([{"other": []}]))

        assert result["_background"] == []
