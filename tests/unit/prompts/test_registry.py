# tests/unit/prompts/test_registry.py
"""
Tests for PromptRegistry (UF 18 Part 5).

Tests prompt registry and versioning.
"""

import json
import pytest
from pathlib import Path
from datetime import datetime

from prompts.registry import (
    PromptRegistry,
    PromptEntry,
    PromptMeta,
)


class TestPromptMeta:
    """Test PromptMeta dataclass."""

    def test_from_raw_minimal(self):
        """Create PromptMeta from minimal raw dict."""
        raw = {
            "source": "manual",
        }

        meta = PromptMeta.from_raw("test_prompt", raw)
        assert meta.prompt_id == "test_prompt"
        assert meta.source == "manual"
        assert meta.version == 1  # default

    def test_from_raw_full(self):
        """Create PromptMeta from full raw dict."""
        raw = {
            "base_id": "v3_stable",
            "created_at": "2024-01-01T12:00:00",
            "source": "auto_evolver",
            "reviewer": "alice",
            "signature": "sig123",
            "version": 2,
        }

        meta = PromptMeta.from_raw("test_prompt", raw)
        assert meta.base_id == "v3_stable"
        assert meta.source == "auto_evolver"
        assert meta.version == 2


class TestPromptRegistry:
    """Test PromptRegistry."""

    def test_load_from_minimal_spec(self):
        """Load from minimal prompt spec."""
        spec = {
            "_version": "1.0",
            "v3_stable": "You are a stable prompt",
        }

        registry = PromptRegistry(spec)
        assert registry.exists("v3_stable")
        assert registry.get_text("v3_stable") == "You are a stable prompt"

    def test_load_from_full_spec(self, tmp_path):
        """Load from full prompt spec with meta."""
        spec = {
            "_version": "1.0",
            "_comment": "Test prompts",
            "v3_stable": "Base prompt v3",
            "v3_fast": "Fast prompt v3",
            "prompts": {
                "v3_stable_tactical_001": "Tactical variant 001",
                "v3_stable_tactical_002": "Tactical variant 002",
            },
            "meta": {
                "v3_stable_tactical_001": {
                    "base_id": "v3_stable",
                    "created_at": "2024-01-01T12:00:00",
                    "source": "auto_evolver",
                    "version": 1,
                },
                "v3_stable_tactical_002": {
                    "base_id": "v3_stable",
                    "created_at": "2024-01-02T12:00:00",
                    "source": "auto_evolver",
                    "version": 2,
                },
            },
        }

        # Save to file
        json_path = tmp_path / "prompts.json"
        json_path.write_text(json.dumps(spec))

        # Load
        registry = PromptRegistry.from_file(json_path)

        assert registry.count_prompts() == 4
        assert registry.exists("v3_stable")
        assert registry.exists("v3_stable_tactical_001")

    def test_list_all_ids(self):
        """List all prompt IDs."""
        spec = {
            "v3_stable": "Prompt 1",
            "v3_fast": "Prompt 2",
            "prompts": {
                "tactical_001": "Tactical 1",
            },
        }

        registry = PromptRegistry(spec)
        ids = registry.list_all_ids()

        assert len(ids) == 3
        assert "v3_stable" in ids
        assert "tactical_001" in ids

    def test_list_by_base(self):
        """List tactical variants by base_id."""
        spec = {
            "v3_stable": "Base",
            "prompts": {
                "v3_stable_tactical_001": "Tactical 1",
                "v3_stable_tactical_002": "Tactical 2",
                "v3_fast_tactical_001": "Other base",
            },
            "meta": {
                "v3_stable_tactical_001": {"base_id": "v3_stable"},
                "v3_stable_tactical_002": {"base_id": "v3_stable"},
                "v3_fast_tactical_001": {"base_id": "v3_fast"},
            },
        }

        registry = PromptRegistry(spec)
        variants = registry.list_by_base("v3_stable")

        assert len(variants) == 2
        assert "v3_stable_tactical_001" in variants
        assert "v3_stable_tactical_002" in variants

    def test_latest_by_base(self):
        """Get latest tactical variant by base_id."""
        spec = {
            "prompts": {
                "v3_stable_tactical_001": "Tactical 1",
                "v3_stable_tactical_002": "Tactical 2",
            },
            "meta": {
                "v3_stable_tactical_001": {
                    "base_id": "v3_stable",
                    "created_at": "2024-01-01T12:00:00",
                },
                "v3_stable_tactical_002": {
                    "base_id": "v3_stable",
                    "created_at": "2024-01-02T12:00:00",  # Later
                },
            },
        }

        registry = PromptRegistry(spec)
        latest = registry.latest_by_base("v3_stable")

        assert latest is not None
        assert latest.prompt_id == "v3_stable_tactical_002"

    def test_latest_by_base_no_variants(self):
        """Return None when no tactical variants exist."""
        spec = {
            "v3_stable": "Base prompt",
        }

        registry = PromptRegistry(spec)
        latest = registry.latest_by_base("v3_stable")

        assert latest is None

    def test_get_nonexistent_prompt_raises(self):
        """Getting nonexistent prompt raises KeyError."""
        spec = {"v3_stable": "Prompt"}
        registry = PromptRegistry(spec)

        with pytest.raises(KeyError, match="Unknown prompt_id"):
            registry.get("nonexistent")

    def test_get_meta(self):
        """Get prompt metadata."""
        spec = {
            "prompts": {
                "test_prompt": "Text",
            },
            "meta": {
                "test_prompt": {
                    "base_id": "v3",
                    "source": "manual",
                    "version": 1,
                },
            },
        }

        registry = PromptRegistry(spec)
        meta = registry.get_meta("test_prompt")

        assert meta is not None
        assert meta.base_id == "v3"
        assert meta.source == "manual"

    def test_prompt_entry_properties(self):
        """Test PromptEntry properties."""
        meta = PromptMeta(
            prompt_id="test",
            base_id="v3",
            created_at=datetime(2024, 1, 1),
            source="manual",
            reviewer="",
            signature="",
            version=1,
        )

        entry = PromptEntry(
            prompt_id="test",
            text="Prompt text",
            meta=meta,
        )

        assert entry.base_id == "v3"
        assert entry.created_at == datetime(2024, 1, 1)
