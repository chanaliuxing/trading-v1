# ai_brain/tools_manifest.py

"""
Tools Manifest Loader.

Loads and provides access to the AI Brain tools manifest,
which defines which actions/tools the brain is allowed to use.

Based on upgradeFeatures15 specification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


_DEFAULT_PATH = Path(__file__).with_name("tools_manifest.json")


def load_tools_manifest(path: Path | str | None = None) -> Dict[str, Any]:
    """
    Load tools manifest from JSON file.

    Args:
        path: Optional custom path to manifest file.
              If None, uses default location (ai_brain/tools_manifest.json)

    Returns:
        Dict containing tools manifest

    Raises:
        FileNotFoundError: If manifest file not found
        json.JSONDecodeError: If manifest is invalid JSON
    """
    if path is None:
        path = _DEFAULT_PATH
    else:
        path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Tools manifest not found at: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def get_tool_descriptions() -> List[Dict[str, str]]:
    """
    Get simplified tool descriptions for LLM prompt.

    Returns list of tools with id, kind, and description only.

    Returns:
        List of dicts with tool metadata
    """
    manifest = load_tools_manifest()
    tools = manifest.get("tools", [])

    descriptions = []
    for tool in tools:
        descriptions.append(
            {
                "id": tool.get("id", ""),
                "kind": tool.get("kind", ""),
                "description": tool.get("description", ""),
            }
        )

    return descriptions


def get_tool_by_id(tool_id: str) -> Optional[Dict[str, Any]]:
    """
    Get full tool specification by ID.

    Args:
        tool_id: Tool ID (e.g., "pi_propose_and_train")

    Returns:
        Tool specification dict, or None if not found
    """
    manifest = load_tools_manifest()
    tools = manifest.get("tools", [])

    for tool in tools:
        if tool.get("id") == tool_id:
            return tool

    return None


def get_safety_rules() -> Dict[str, str]:
    """
    Get safety rules from manifest.

    Returns:
        Dict of safety rule name -> description
    """
    manifest = load_tools_manifest()
    return manifest.get("safety_rules", {})
