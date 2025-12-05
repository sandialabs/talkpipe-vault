"""Unit tests for the DiagPrint segment."""

import pytest
from talkpipe import compile

from talkpipe_vault.util import DiagPrint


class TestDiagPrint:
    """Test suite for DiagPrint segment."""

    def test_yields_all_items(self):
        """Test that DiagPrint yields all input items unchanged."""
        items = [1, 2, 3, "test", {"key": "value"}]
        pipeline = DiagPrint()
        result = list(pipeline(items))
        assert result == items

    def test_output_to_stdout_by_default(self, capsys):
        """Test that output goes to stdout by default."""
        items = ["hello"]
        pipeline = DiagPrint()
        list(pipeline(items))

        captured = capsys.readouterr()
        assert "hello" in captured.out
        assert captured.err == ""

    def test_output_to_stderr(self, capsys):
        """Test that output goes to stderr when use_stderr=True."""
        items = ["hello"]
        pipeline = DiagPrint(use_stderr=True)
        list(pipeline(items))

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "hello" in captured.err

    def test_prints_type_and_value(self, capsys):
        """Test that type and value are printed."""
        items = [42]
        pipeline = DiagPrint()
        list(pipeline(items))

        captured = capsys.readouterr()
        assert "Type:" in captured.out
        assert "<class 'int'>" in captured.out
        assert "Value: 42" in captured.out

    def test_field_list_parameter(self, capsys):
        """Test that field_list extracts and displays specified fields."""
        items = [{"name": "Alice", "age": 30, "city": "NYC"}]
        pipeline = DiagPrint(field_list="name,age")
        list(pipeline(items))

        captured = capsys.readouterr()
        assert "Fields:" in captured.out
        assert "name: Alice" in captured.out
        assert "age: 30" in captured.out

    def test_expression_parameter(self, capsys):
        """Test that expression is evaluated and printed."""
        items = [10]
        pipeline = DiagPrint(expression="item * 2")
        list(pipeline(items))

        captured = capsys.readouterr()
        assert "Expression:" in captured.out
        assert "Value: 20" in captured.out

    def test_via_pipeline_compile(self, capsys):
        """Test DiagPrint works when invoked via pipeline compilation."""
        pipeline = compile(" | diagPrint")
        result = list(pipeline(["test_item"]))

        assert result == ["test_item"]
        captured = capsys.readouterr()
        assert "test_item" in captured.out

    def test_via_pipeline_with_stderr(self, capsys):
        """Test DiagPrint with use_stderr via pipeline compilation."""
        pipeline = DiagPrint(use_stderr=True)
        result = list(pipeline(["stderr_test"]))

        assert result == ["stderr_test"]
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "stderr_test" in captured.err
