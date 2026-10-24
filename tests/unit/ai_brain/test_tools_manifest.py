# tests/unit/ai_brain/test_tools_manifest.py

"""
Tests for tools manifest loader.

Tests loading and accessing the AI Brain tools manifest.
"""

import json
from pathlib import Path

import pytest

from ai_brain.tools_manifest import (
    get_safety_rules,
    get_tool_by_id,
    get_tool_descriptions,
    load_tools_manifest,
)


class TestLoadToolsManifest:
    """Test load_tools_manifest() function."""

    def test_load_default_manifest(self):
        """Should load default manifest successfully."""
        manifest = load_tools_manifest()

        assert isinstance(manifest, dict)
        assert "_version" in manifest
        assert "tools" in manifest
        assert isinstance(manifest["tools"], list)

    def test_manifest_has_expected_tools(self):
        """Manifest should contain expected tool IDs."""
        manifest = load_tools_manifest()
        tools = manifest["tools"]

        tool_ids = [tool["id"] for tool in tools]

        assert "pi_propose_and_train" in tool_ids
        assert "skip_training" in tool_ids
        assert "ask_human" in tool_ids
        assert "reuse_existing_model" in tool_ids

    def test_manifest_has_safety_rules(self):
        """Manifest should contain safety_rules section."""
        manifest = load_tools_manifest()

        assert "safety_rules" in manifest
        assert isinstance(manifest["safety_rules"], dict)
        assert "no_arbitrary_sql" in manifest["safety_rules"]

    def test_load_nonexistent_manifest_fails(self):
        """Loading nonexistent manifest should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_tools_manifest("/nonexistent/path/manifest.json")


class TestGetToolDescriptions:
    """Test get_tool_descriptions() function."""

    def test_get_descriptions(self):
        """Should return simplified tool descriptions."""
        descriptions = get_tool_descriptions()

        assert isinstance(descriptions, list)
        assert len(descriptions) > 0

        # Each description should have id, kind, description
        for desc in descriptions:
            assert "id" in desc
            assert "kind" in desc
            assert "description" in desc

    def test_descriptions_match_manifest_count(self):
        """Number of descriptions should match manifest tools count."""
        manifest = load_tools_manifest()
        descriptions = get_tool_descriptions()

        assert len(descriptions) == len(manifest["tools"])


class TestGetToolById:
    """Test get_tool_by_id() function."""

    def test_get_existing_tool(self):
        """Should return tool specification for existing ID."""
        tool = get_tool_by_id("pi_propose_and_train")

        assert tool is not None
        assert tool["id"] == "pi_propose_and_train"
        assert tool["kind"] == "pi_pipeline"
        assert "description" in tool
        assert "input_schema" in tool
        assert "output" in tool

    def test_get_skip_training_tool(self):
        """Should return skip_training tool."""
        tool = get_tool_by_id("skip_training")

        assert tool is not None
        assert tool["kind"] == "meta"
        assert tool["input_schema"] == "none"

    def test_get_nonexistent_tool_returns_none(self):
        """Should return None for nonexistent tool ID."""
        tool = get_tool_by_id("nonexistent_tool")

        assert tool is None


class TestGetSafetyRules:
    """Test get_safety_rules() function."""

    def test_get_safety_rules(self):
        """Should return safety rules dict."""
        rules = get_safety_rules()

        assert isinstance(rules, dict)
        assert len(rules) > 0

    def test_safety_rules_content(self):
        """Safety rules should contain expected rules."""
        rules = get_safety_rules()

        assert "no_arbitrary_sql" in rules
        assert "no_arbitrary_labels" in rules
        assert "no_system_changes" in rules
        assert "audit_trail_required" in rules

    def test_safety_rules_have_descriptions(self):
        """Each safety rule should have a description."""
        rules = get_safety_rules()

        for rule_name, description in rules.items():
            assert isinstance(description, str)
            assert len(description) > 0


class TestManifestStructure:
    """Test overall manifest structure and content."""

    def test_manifest_version(self):
        """Manifest should have version number."""
        manifest = load_tools_manifest()

        assert manifest["_version"] == 1

    def test_action_precedence(self):
        """Manifest should define action precedence."""
        manifest = load_tools_manifest()

        assert "action_precedence" in manifest
        precedence = manifest["action_precedence"]
        assert "order" in precedence
        assert isinstance(precedence["order"], list)

    def test_validation_layers(self):
        """Manifest should define validation layers."""
        manifest = load_tools_manifest()

        assert "validation_layers" in manifest
        layers = manifest["validation_layers"]
        assert isinstance(layers, dict)
        assert "layer_1" in layers
        assert "Pydantic" in layers["layer_1"]

    def test_pi_tool_constraints(self):
        """PI tool should have defined constraints."""
        tool = get_tool_by_id("pi_propose_and_train")

        assert "constraints" in tool
        constraints = tool["constraints"]
        assert "max_training_rows" in constraints
        assert "max_features" in constraints
        assert "allowed_model_types" in constraints

    def test_pi_tool_safety(self):
        """PI tool should have defined safety features."""
        tool = get_tool_by_id("pi_propose_and_train")

        assert "safety" in tool
        safety = tool["safety"]
        assert "label_source" in safety
        assert "LabelRegistry" in safety["label_source"]
        assert "data_checks" in safety
        assert "TrainingGate" in safety["data_checks"]
