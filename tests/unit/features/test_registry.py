"""Tests for feature registry."""

import json
import tempfile
from pathlib import Path

import pytest

from features.registry import FeatureDef, FeatureRegistry, get_default_registry


class TestFeatureDef:
    """Test FeatureDef dataclass."""

    def test_feature_def_creation(self):
        """Test creating a feature definition."""
        fd = FeatureDef(
            name="ret_1b",
            kind="python",
            freq=["1m"],
            base_cols=["close"],
            tags=["return", "short_horizon"],
            desc="1 bar return",
            leak_safe=True,
            default_exposed_to_llm=True,
        )

        assert fd.name == "ret_1b"
        assert fd.kind == "python"
        assert fd.freq == ["1m"]
        assert fd.base_cols == ["close"]
        assert fd.tags == ["return", "short_horizon"]
        assert fd.leak_safe is True
        assert fd.default_exposed_to_llm is True


class TestFeatureRegistry:
    """Test FeatureRegistry class."""

    @pytest.fixture
    def sample_registry_data(self):
        """Create sample registry data."""
        return {
            "version": "1.0",
            "features": {
                "close": {
                    "kind": "native",
                    "freq": ["1m"],
                    "base_cols": ["close"],
                    "tags": ["price", "base"],
                    "desc": "Close price",
                    "leak_safe": True,
                    "default_exposed_to_llm": True,
                },
                "ret_1b": {
                    "kind": "python",
                    "freq": ["1m"],
                    "base_cols": ["close"],
                    "tags": ["return"],
                    "desc": "1 bar return",
                    "leak_safe": True,
                    "default_exposed_to_llm": True,
                },
                "internal_feature": {
                    "kind": "python",
                    "freq": ["1m"],
                    "base_cols": ["close", "volume"],
                    "tags": ["internal"],
                    "desc": "Internal feature",
                    "leak_safe": True,
                    "default_exposed_to_llm": False,  # Not exposed
                },
            },
        }

    @pytest.fixture
    def registry_file(self, sample_registry_data, tmp_path):
        """Create a temporary registry file."""
        registry_path = tmp_path / "feature_registry.json"
        registry_path.write_text(json.dumps(sample_registry_data))
        return registry_path

    def test_from_json_loads_correctly(self, registry_file):
        """Test loading registry from JSON file."""
        registry = FeatureRegistry.from_json(registry_file)

        assert registry.version == "1.0"
        assert len(registry.all_names()) == 3
        assert "close" in registry.all_names()
        assert "ret_1b" in registry.all_names()
        assert "internal_feature" in registry.all_names()

    def test_from_json_missing_file(self, tmp_path):
        """Test loading from non-existent file returns empty registry."""
        missing_path = tmp_path / "missing.json"
        registry = FeatureRegistry.from_json(missing_path)

        assert registry.version == "0.0"
        assert len(registry.all_names()) == 0

    def test_get_feature(self, registry_file):
        """Test getting a feature definition."""
        registry = FeatureRegistry.from_json(registry_file)

        feature = registry.get("ret_1b")
        assert feature is not None
        assert feature.name == "ret_1b"
        assert feature.kind == "python"
        assert feature.base_cols == ["close"]

    def test_get_unknown_feature(self, registry_file):
        """Test getting an unknown feature returns None."""
        registry = FeatureRegistry.from_json(registry_file)

        feature = registry.get("unknown_feature")
        assert feature is None

    def test_list_for_freq(self, registry_file):
        """Test listing features for a frequency."""
        registry = FeatureRegistry.from_json(registry_file)

        features = registry.list_for_freq("1m", exposed_only=False)
        assert len(features) == 3

        features_exposed = registry.list_for_freq("1m", exposed_only=True)
        assert len(features_exposed) == 2
        assert all(f.default_exposed_to_llm for f in features_exposed)

    def test_list_for_freq_no_match(self, registry_file):
        """Test listing features for frequency with no matches."""
        registry = FeatureRegistry.from_json(registry_file)

        features = registry.list_for_freq("5m")
        assert len(features) == 0

    def test_summarize_for_llm(self, registry_file):
        """Test generating LLM summary."""
        registry = FeatureRegistry.from_json(registry_file)

        summary = registry.summarize_for_llm("1m")
        assert "close" in summary
        assert "ret_1b" in summary
        assert "internal_feature" not in summary  # Not exposed
        assert "Close price" in summary
        assert "1 bar return" in summary

    def test_summarize_for_llm_empty(self, registry_file):
        """Test generating LLM summary for frequency with no features."""
        registry = FeatureRegistry.from_json(registry_file)

        summary = registry.summarize_for_llm("5m")
        assert "No curated features" in summary


class TestGetDefaultRegistry:
    """Test get_default_registry function."""

    def test_get_default_registry_loads_real_file(self):
        """Test that default registry loads from actual config file."""
        # This will load from config/feature_registry.json
        registry = get_default_registry()

        # Should have the features we created
        assert registry.version == "1.0"
        assert "close" in registry.all_names()
        assert "ret_1b" in registry.all_names()
        assert "ma_20_close" in registry.all_names()

    def test_get_default_registry_with_custom_dir(self, tmp_path):
        """Test loading registry from custom directory."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        registry_data = {
            "version": "2.0",
            "features": {
                "test_feature": {
                    "kind": "native",
                    "freq": ["1m"],
                    "base_cols": ["test"],
                    "tags": ["test"],
                    "desc": "Test feature",
                    "leak_safe": True,
                    "default_exposed_to_llm": True,
                }
            },
        }

        registry_path = config_dir / "feature_registry.json"
        registry_path.write_text(json.dumps(registry_data))

        registry = get_default_registry(config_dir)
        assert registry.version == "2.0"
        assert "test_feature" in registry.all_names()
