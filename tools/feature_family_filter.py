#!/usr/bin/env python3
from __future__ import annotations
"""
Feature family filter utilities.

Allows OOF pipelines to respect high-level feature family toggles defined in:
  - config/feature_families.json
  - config/feature_registry.json

If configs are missing or invalid, the filter is a no-op (returns input cols).
"""

import json
from pathlib import Path
from typing import Dict, List, Set


def _load_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


def _load_feature_registry() -> Dict[str, List[str]]:
    """Return mapping col_name -> tags (from feature_registry.json)."""
    p = Path("config/feature_registry.json")
    data = _load_json(p)
    feats = data.get("features") or {}
    col_to_tags: Dict[str, List[str]] = {}
    if isinstance(feats, dict):
        for name, meta in feats.items():
            if not isinstance(meta, dict):
                continue
            tags = meta.get("tags") or []
            if isinstance(tags, list):
                col_to_tags[str(name)] = [str(t) for t in tags]
    return col_to_tags


def _load_feature_families() -> Dict[str, Dict]:
    """Return families config from feature_families.json."""
    p = Path("config/feature_families.json")
    data = _load_json(p)
    fams = data.get("families") or {}
    if isinstance(fams, dict):
        return fams
    return {}


def filter_feature_columns(cols: List[str]) -> List[str]:
    """
    Filter feature column names according to enabled families.

    - If config/feature_families.json or feature_registry.json is missing,
      returns cols unchanged.
    - Columns not present in feature_registry.json are kept by default.
    - A column is kept if it has any tag that belongs to at least one
      enabled family.
    """
    if not cols:
        return cols

    col_to_tags = _load_feature_registry()
    families = _load_feature_families()
    if not families or not col_to_tags:
        # Nothing to filter against
        return cols

    enabled_tags: Set[str] = set()
    for fam_cfg in families.values():
        try:
            if not isinstance(fam_cfg, dict):
                continue
            if not bool(fam_cfg.get("enabled", True)):
                continue
            tags = fam_cfg.get("tags") or []
            if isinstance(tags, list):
                for t in tags:
                    enabled_tags.add(str(t))
        except Exception:
            continue

    # If no tags are enabled, do not drop everything: fail open and return cols
    if not enabled_tags:
        return cols

    filtered: List[str] = []
    for c in cols:
        cname = str(c)
        tags = col_to_tags.get(cname)
        if not tags:
            # Column not in registry – keep it to avoid accidental data loss
            filtered.append(cname)
            continue
        if any(t in enabled_tags for t in tags):
            filtered.append(cname)

    # If filtering removed everything (e.g. misconfigured tags), fall back
    # to original cols to avoid breaking the pipeline.
    return filtered or cols


if __name__ == "__main__":
    # Simple smoke-test when run directly
    example = ["open", "close", "volume", "day_of_week", "unknown_feature"]
    print("Input cols:", example)
    print("Filtered cols:", filter_feature_columns(example))

