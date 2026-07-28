"""Unit tests for the keyword-augmented retrieval segments.

These tests exercise keyword-query parsing and search-result merging without a
vault, an embedding model, or a reachable LLM (the eliza source stands in for
the LLM where one is structurally required), so they run in every environment.
"""

from talkpipe import compile
from talkpipe.search.abstract import SearchResult

from talkpipe_vault.pipelines.searching_and_prompting import (
    ExtractSearchKeywords,
    MergeSearchResults,
    keyword_query_from_llm_output,
)


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
