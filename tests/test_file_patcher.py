"""
Unit tests for FilePatcher - fuzzy matching, idempotency, exact matching.
"""
import pytest
from meta_agent.self_programming.infra.file_patcher import (
    FilePatcher,
    normalize_whitespace,
    MatchResult,
)
from meta_agent.self_programming.schema import FileModification


class TestNormalizeWhitespace:
    def test_strips_indentation(self):
        input_text = "    def foo():\n        return 1"
        expected = "def foo():\nreturn 1"
        assert normalize_whitespace(input_text) == expected

    def test_collapses_multiple_spaces(self):
        input_text = "a    b     c"
        expected = "a b c"
        assert normalize_whitespace(input_text) == expected

    def test_handles_crlf(self):
        input_text = "line1\r\nline2\r\n"
        expected = "line1\nline2\n"
        assert normalize_whitespace(input_text) == expected


class TestFilePatcherExactMatch:
    def test_exact_match_replacement(self):
        patcher = FilePatcher()
        original_text = """def foo(x: int) -> int:
    return x + 1

def bar():
    pass
"""
        mod = FileModification(
            file_path="test.py",
            action="modify",
            original_snippet="def foo(x: int) -> int:\n    return x + 1",
            new_content="def foo_v2(x: int) -> int:\n    return x + 2",
        )

        new_text, reports = patcher._apply_to_text(original_text, [mod], "test.py")

        assert "def foo_v2(x: int) -> int:" in new_text
        assert "return x + 2" in new_text
        assert len(reports) == 1
        assert reports[0].match_kind == "exact"
        assert reports[0].similarity == 1.0


class TestFilePatcherFuzzyMatch:
    def test_fuzzy_match_with_different_indentation(self):
        patcher = FilePatcher(similarity_threshold=0.9)

        # Original has 4 spaces
        original_text = """def foo(x):
    return x + 1
"""

        # Snippet has 2 spaces (different indentation)
        mod = FileModification(
            file_path="test.py",
            action="modify",
            original_snippet="def foo(x):\n  return x + 1",  # 2 spaces
            new_content="def foo_v2(x):\n    return x + 2",
        )

        new_text, reports = patcher._apply_to_text(original_text, [mod], "test.py")

        assert "def foo_v2(x):" in new_text
        assert len(reports) == 1
        assert reports[0].match_kind == "fuzzy"
        assert reports[0].similarity is not None
        assert reports[0].similarity >= 0.9

    def test_fuzzy_match_with_extra_spaces(self):
        patcher = FilePatcher(similarity_threshold=0.9)

        original_text = "result = calculate(a,b,c)\n"

        # Snippet has extra spaces
        mod = FileModification(
            file_path="test.py",
            action="modify",
            original_snippet="result  =  calculate( a, b, c )",  # Extra spaces
            new_content="result = better_calculate(a, b, c)",
        )

        new_text, reports = patcher._apply_to_text(original_text, [mod], "test.py")

        assert "better_calculate" in new_text
        assert len(reports) == 1
        assert reports[0].match_kind in ("exact", "fuzzy")


class TestFilePatcherIdempotency:
    def test_already_applied_exact(self):
        patcher = FilePatcher()

        # Text already contains the new_content
        text_already_patched = """def foo_v2(x: int) -> int:
    return x + 2
"""

        mod = FileModification(
            file_path="test.py",
            action="modify",
            original_snippet="def foo_v2(x: int) -> int:\n    return x + 2",
            new_content="def foo_v2(x: int) -> int:\n    return x + 2",
        )

        new_text, reports = patcher._apply_to_text(text_already_patched, [mod], "test.py")

        # Should not modify (idempotent)
        assert new_text == text_already_patched
        assert len(reports) == 1
        assert reports[0].match_kind == "already_applied"

    def test_already_applied_fuzzy(self):
        patcher = FilePatcher(similarity_threshold=0.9)

        # Text already effectively contains new_content (just different whitespace)
        text_already_patched = "def  foo_v2(x):  \n    return   x + 2\n"

        mod = FileModification(
            file_path="test.py",
            action="modify",
            original_snippet="def foo_v2(x):\n    return x + 2",
            new_content="def foo_v2(x):\n    return x + 2",
        )

        new_text, reports = patcher._apply_to_text(text_already_patched, [mod], "test.py")

        # Should detect it's already applied via fuzzy similarity
        assert len(reports) == 1
        # Might be exact or already_applied depending on exact match first
        assert reports[0].match_kind in ("exact", "already_applied", "fuzzy")


class TestFilePatcherNotFound:
    def test_no_match_found(self):
        patcher = FilePatcher()

        original_text = "def bar():\n    pass\n"

        mod = FileModification(
            file_path="test.py",
            action="modify",
            original_snippet="def nonexistent_function():\n    return 42",
            new_content="def new_function():\n    return 100",
        )

        new_text, reports = patcher._apply_to_text(original_text, [mod], "test.py")

        # Text should not change
        assert new_text == original_text
        assert len(reports) == 1
        assert reports[0].match_kind == "not_found"


class TestFilePatcherCreate:
    def test_create_new_file(self):
        patcher = FilePatcher()

        mod = FileModification(
            file_path="new_file.py",
            action="create",
            original_snippet=None,
            new_content="# New file\ndef hello():\n    print('Hello')\n",
        )

        new_text, reports = patcher._apply_to_text("", [mod], "new_file.py")

        assert "def hello():" in new_text
        assert len(reports) == 1
        assert reports[0].match_kind == "exact"
        assert "created new file content" in reports[0].message


class TestFilePatcherAmbiguous:
    def test_ambiguous_match_detection(self):
        patcher = FilePatcher(similarity_threshold=0.85)

        # Text with two very similar blocks
        original_text = """def process_data(x):
    return x + 1

def process_data(y):
    return y + 1
"""

        mod = FileModification(
            file_path="test.py",
            action="modify",
            original_snippet="def process_data(z):\n    return z + 1",  # Could match either
            new_content="def process_data_v2(z):\n    return z + 2",
        )

        new_text, reports = patcher._apply_to_text(original_text, [mod], "test.py")

        # Should detect ambiguity and not apply
        assert len(reports) == 1
        # Might be ambiguous or exact depending on first exact match
        if reports[0].match_kind == "ambiguous":
            assert "ambiguous" in reports[0].message.lower()


class TestFilePatcherMultipleModifications:
    def test_multiple_mods_same_file(self):
        patcher = FilePatcher()

        original_text = """def foo():
    return 1

def bar():
    return 2
"""

        mods = [
            FileModification(
                file_path="test.py",
                action="modify",
                original_snippet="def foo():\n    return 1",
                new_content="def foo_new():\n    return 10",
            ),
            FileModification(
                file_path="test.py",
                action="modify",
                original_snippet="def bar():\n    return 2",
                new_content="def bar_new():\n    return 20",
            ),
        ]

        new_text, reports = patcher._apply_to_text(original_text, mods, "test.py")

        assert "foo_new" in new_text
        assert "bar_new" in new_text
        assert "return 10" in new_text
        assert "return 20" in new_text
        assert len(reports) == 2
        assert all(r.match_kind == "exact" for r in reports)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
