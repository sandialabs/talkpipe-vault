"""Unit tests for per-vault retrieval-filter storage and validation.

These exercise the script sidecar and the ChatterLang structural checks
without a vault, an embedding model, or a reachable LLM, so they run in every
environment.
"""

import pytest

from talkpipe_vault.pipelines import retrieval_filter

KEEP_NON_DRAFT = (
    "| lambdaFilter[expression=\"'draft' not in document.get('content', '')\"]"
)

RESULTS = [
    {"doc_id": "a", "score": 0.9, "document": {"content": "keep this"}},
    {"doc_id": "b", "score": 0.2, "document": {"content": "draft notes"}},
]


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
