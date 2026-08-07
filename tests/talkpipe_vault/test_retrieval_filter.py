"""Unit tests for per-vault retrieval-filter storage and validation.

These exercise the script sidecar and the ChatterLang structural checks
without a vault, an embedding model, or a reachable LLM, so they run in every
environment.
"""

import html
import re
from pathlib import Path

import pytest

import talkpipe_vault.apps
from talkpipe_vault.pipelines import retrieval_filter

KEEP_NON_DRAFT = (
    "| lambdaFilter[expression=\"'draft' not in item['document'].get('content', '')\"]"
)

RESULTS = [
    {
        "doc_id": "a",
        "score": 0.9,
        "document": {
            "content": "keep this",
            "source": "/notes/a.txt",
            "title": "a.txt",
        },
    },
    {
        "doc_id": "b",
        "score": 0.2,
        "document": {
            "content": "draft notes",
            "source": "/notes/b.txt",
            "title": "b.txt",
        },
    },
]

DOCUMENTS_TEMPLATE = (
    Path(talkpipe_vault.apps.__file__).parent / "templates" / "documents.html"
)


class TestScriptStorage:
    def test_save_then_load_round_trips(self, tmp_path):
        retrieval_filter.save_script(str(tmp_path), KEEP_NON_DRAFT)
        assert retrieval_filter.load_script(str(tmp_path)) == KEEP_NON_DRAFT

    def test_script_lives_in_the_vault_folder(self, tmp_path):
        retrieval_filter.save_script(str(tmp_path), KEEP_NON_DRAFT)
        path = retrieval_filter.script_path(str(tmp_path))
        assert path.parent == tmp_path
        assert path.name == retrieval_filter.FILTER_FILENAME

    def test_save_leaves_no_temp_file_behind(self, tmp_path):
        retrieval_filter.save_script(str(tmp_path), KEEP_NON_DRAFT)
        assert [p.name for p in tmp_path.iterdir()] == [
            retrieval_filter.FILTER_FILENAME
        ]

    def test_absent_script_reads_as_none(self, tmp_path):
        assert retrieval_filter.load_script(str(tmp_path)) is None

    def test_blank_script_reads_as_none(self, tmp_path):
        retrieval_filter.save_script(str(tmp_path), "   \n\n")
        assert retrieval_filter.load_script(str(tmp_path)) is None

    def test_unreadable_script_reads_as_none(self, tmp_path, monkeypatch):
        """A broken sidecar must never keep a vault from opening."""
        retrieval_filter.save_script(str(tmp_path), KEEP_NON_DRAFT)

        def _boom(*args, **kwargs):
            raise OSError("unreadable")

        monkeypatch.setattr(retrieval_filter.Path, "read_text", _boom)
        assert retrieval_filter.load_script(str(tmp_path)) is None

    def test_remove_reports_whether_a_script_existed(self, tmp_path):
        assert retrieval_filter.remove_script(str(tmp_path)) is False
        retrieval_filter.save_script(str(tmp_path), KEEP_NON_DRAFT)
        assert retrieval_filter.remove_script(str(tmp_path)) is True
        assert retrieval_filter.load_script(str(tmp_path)) is None


class TestValidateScript:
    def test_segment_only_pipeline_is_accepted(self):
        assert retrieval_filter.validate_script(KEEP_NON_DRAFT) is None

    def test_empty_script_is_rejected(self):
        assert "empty" in retrieval_filter.validate_script("   ").lower()

    def test_input_source_is_rejected_with_a_specific_message(self):
        error = retrieval_filter.validate_script('INPUT FROM echo[data="a"] | toDict')
        assert "input source" in error.lower()

    def test_more_than_one_pipeline_is_rejected(self):
        error = retrieval_filter.validate_script(
            '| lambdaFilter[expression="score > 0.5"] ; | toDict'
        )
        assert "single pipeline" in error.lower()

    def test_unknown_segment_is_rejected(self):
        error = retrieval_filter.validate_script("| noSuchSegmentExists")
        assert "noSuchSegmentExists" in error

    def test_syntax_error_is_reported(self):
        assert retrieval_filter.validate_script("| | |") is not None


class TestCompileScript:
    def test_compiled_script_filters_the_result_stream(self):
        filter_fn = retrieval_filter.compile_script(KEEP_NON_DRAFT)
        kept = list(filter_fn(iter(RESULTS)))
        assert [item["doc_id"] for item in kept] == ["a"]

    def test_unusable_script_raises_the_validation_message(self):
        with pytest.raises(ValueError, match="noSuchSegmentExists"):
            retrieval_filter.compile_script("| noSuchSegmentExists")


class TestExpressionNaming:
    """Both ways of naming a result in a lambda expression must keep working.

    ``item`` is TalkPipe's name for the value flowing through a lambda; because
    each result is a dict, TalkPipe additionally exposes its top-level keys as
    bare names. The UI help documents ``item`` and mentions the shorthand, so
    both are pinned here (issue #25).
    """

    @pytest.mark.parametrize(
        "expression",
        [
            "'draft' not in item['document'].get('content', '')",
            "'draft' not in document.get('content', '')",
            "item['score'] > 0.5",
            "score > 0.5",
        ],
    )
    def test_expression_selects_the_first_result(self, expression):
        script = f'| lambdaFilter[expression="{expression}"]'
        assert retrieval_filter.validate_script(script) is None
        filter_fn = retrieval_filter.compile_script(script)
        kept = list(filter_fn(iter([dict(entry) for entry in RESULTS])))
        assert [entry["doc_id"] for entry in kept] == ["a"]


class TestContainmentSegments:
    """``isIn``/``isNotIn`` as the expression-free form of a containment filter.

    The UI and ADVANCED.md offer these as the simple way to say "keep/drop
    results whose field contains this text" (issue #27), so their behaviour —
    including the two ways they differ from ``lambdaFilter`` — is pinned here.
    """

    @staticmethod
    def _run(script: str, results=None) -> list[str]:
        assert retrieval_filter.validate_script(script) is None, script
        filter_fn = retrieval_filter.compile_script(script)
        entries = [dict(entry) for entry in (results if results else RESULTS)]
        return [entry["doc_id"] for entry in filter_fn(iter(entries))]

    def test_is_not_in_drops_results_whose_content_contains_the_text(self):
        assert self._run('| isNotIn[field="document.content", value="draft"]') == ["a"]

    def test_is_in_keeps_results_whose_content_contains_the_text(self):
        assert self._run('| isIn[field="document.content", value="draft"]') == ["b"]

    def test_dotted_path_reaches_the_source_of_a_result(self):
        assert self._run('| isIn[field="document.source", value="/notes/b"]') == ["b"]

    def test_containment_segments_chain(self):
        script = (
            '| isIn[field="document.source", value="/notes/"]'
            ' | isNotIn[field="document.content", value="draft"]'
        )
        assert self._run(script) == ["a"]

    def test_matching_is_case_sensitive(self):
        """Documented difference from the .lower() lambdaFilter recipe."""
        assert self._run('| isIn[field="document.content", value="Draft"]') == []

    def test_a_missing_field_fails_the_script(self):
        """The other documented difference: no .get(...) default to fall back on."""
        results = [{"doc_id": "c", "score": 0.5, "document": {"source": "/notes/c"}}]
        with pytest.raises(TypeError):
            self._run('| isNotIn[field="document.content", value="draft"]', results)


class TestDocumentedRecipes:
    """The one-click starter recipes in the UI must actually run (issue #25)."""

    @staticmethod
    def _recipes() -> list[str]:
        markup = DOCUMENTS_TEMPLATE.read_text(encoding="utf-8")
        return [
            html.unescape(match)
            for match in re.findall(
                r'class="btn-secondary filter-insert"\s+data-script="([^"]*)"', markup
            )
        ]

    def test_recipes_are_present(self):
        assert len(self._recipes()) >= 3

    def test_containment_recipes_are_offered(self):
        """The expression-free form has starter recipes too (issue #27)."""
        assert any(
            "isIn[" in recipe or "isNotIn[" in recipe for recipe in self._recipes()
        )

    def test_every_recipe_compiles_and_runs(self):
        for recipe in self._recipes():
            assert retrieval_filter.validate_script(recipe) is None, recipe
            filter_fn = retrieval_filter.compile_script(recipe)
            kept = list(filter_fn(iter([dict(entry) for entry in RESULTS])))
            assert all(isinstance(entry, dict) for entry in kept), recipe
