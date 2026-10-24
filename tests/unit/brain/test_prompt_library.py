"""Tests for prompt library loader."""

import json
import tempfile
from pathlib import Path

import pytest

from brain.prompt_library import PromptLibrary, load_prompt_library


class TestPromptLibrary:
    """Test PromptLibrary class."""

    def test_get_prompt_success(self):
        """Test getting a prompt that exists."""
        prompts = {"v3_stable": "This is a stable prompt"}
        lib = PromptLibrary(raw={}, prompts=prompts, meta={})

        assert lib.get("v3_stable") == "This is a stable prompt"

    def test_get_prompt_not_found(self):
        """Test getting a prompt that doesn't exist raises KeyError."""
        lib = PromptLibrary(raw={}, prompts={}, meta={})

        with pytest.raises(KeyError) as exc_info:
            lib.get("nonexistent")

        assert "not found" in str(exc_info.value)

    def test_get_meta_success(self):
        """Test getting metadata for a prompt."""
        meta = {"v3_stable": {"version": "3.0", "created": "2025-01-01"}}
        lib = PromptLibrary(raw={}, prompts={}, meta=meta)

        metadata = lib.get_meta("v3_stable")
        assert metadata["version"] == "3.0"
        assert metadata["created"] == "2025-01-01"

    def test_get_meta_not_found(self):
        """Test getting metadata for non-existent prompt returns empty dict."""
        lib = PromptLibrary(raw={}, prompts={}, meta={})

        metadata = lib.get_meta("nonexistent")
        assert metadata == {}


class TestLoadPromptLibrary:
    """Test load_prompt_library function."""

    @pytest.fixture
    def simple_prompt_file(self, tmp_path):
        """Create a simple prompt library file."""
        data = {
            "_comment": "Test prompts",
            "v3_stable": "You are a conservative trader.",
            "v3_aggressive": "You are an aggressive trader.",
        }
        path = tmp_path / "prompts.json"
        path.write_text(json.dumps(data))
        return path

    @pytest.fixture
    def nested_prompt_file(self, tmp_path):
        """Create a nested prompt library file."""
        data = {
            "_comment": "Test prompts",
            "_usage": "Use these prompts wisely",
            "v3_stable": "Top-level stable prompt",
            "prompts": {
                "v3_tactical_20251117": "Tactical prompt from Nov 17",
                "v4_experimental": "Experimental prompt",
            },
            "meta": {
                "v3_tactical_20251117": {"version": "3.1", "date": "2025-11-17"},
                "v4_experimental": {"version": "4.0", "experimental": True},
            },
        }
        path = tmp_path / "prompts.json"
        path.write_text(json.dumps(data))
        return path

    def test_load_simple_prompts(self, simple_prompt_file):
        """Test loading simple top-level prompts."""
        lib = load_prompt_library(simple_prompt_file)

        assert lib.get("v3_stable") == "You are a conservative trader."
        assert lib.get("v3_aggressive") == "You are an aggressive trader."

    def test_load_nested_prompts(self, nested_prompt_file):
        """Test loading nested prompts."""
        lib = load_prompt_library(nested_prompt_file)

        # Top-level prompts
        assert lib.get("v3_stable") == "Top-level stable prompt"

        # Nested prompts
        assert lib.get("v3_tactical_20251117") == "Tactical prompt from Nov 17"
        assert lib.get("v4_experimental") == "Experimental prompt"

    def test_load_metadata(self, nested_prompt_file):
        """Test loading prompt metadata."""
        lib = load_prompt_library(nested_prompt_file)

        meta_tactical = lib.get_meta("v3_tactical_20251117")
        assert meta_tactical["version"] == "3.1"
        assert meta_tactical["date"] == "2025-11-17"

        meta_exp = lib.get_meta("v4_experimental")
        assert meta_exp["version"] == "4.0"
        assert meta_exp["experimental"] is True

    def test_skips_underscored_keys(self, nested_prompt_file):
        """Test that keys starting with _ are skipped."""
        lib = load_prompt_library(nested_prompt_file)

        with pytest.raises(KeyError):
            lib.get("_comment")

        with pytest.raises(KeyError):
            lib.get("_usage")

    def test_skips_reserved_keys(self, nested_prompt_file):
        """Test that 'prompts' and 'meta' keys are not treated as prompts."""
        lib = load_prompt_library(nested_prompt_file)

        # These should not be in the prompts dict
        with pytest.raises(KeyError):
            lib.get("prompts")

        with pytest.raises(KeyError):
            lib.get("meta")

    def test_empty_library(self, tmp_path):
        """Test loading an empty library."""
        data = {"_comment": "Empty"}
        path = tmp_path / "empty.json"
        path.write_text(json.dumps(data))

        lib = load_prompt_library(path)
        assert len(lib.prompts) == 0

    def test_non_string_values_ignored(self, tmp_path):
        """Test that non-string values are ignored."""
        data = {
            "valid_prompt": "This is valid",
            "invalid_number": 123,
            "invalid_dict": {"key": "value"},
            "invalid_list": ["item1", "item2"],
        }
        path = tmp_path / "mixed.json"
        path.write_text(json.dumps(data))

        lib = load_prompt_library(path)

        assert lib.get("valid_prompt") == "This is valid"

        with pytest.raises(KeyError):
            lib.get("invalid_number")
