#!/usr/bin/env python3
"""
Apply Config Patch from AI Brain

从 AI 大脑输出的 JSON patch 应用到基础配置，生成下一轮 HPO 的配置文件。

Usage:
    python apply_config_patch.py \
        --base alpha_config_enhanced.json \
        --patch next_config_patch.json \
        --output alpha_config_enhanced_next.json
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict
from copy import deepcopy


def apply_dot_path(config: dict, path: str, value: Any) -> None:
    """
    应用 dot-separated path 到嵌套字典

    Example:
        apply_dot_path(config, "optuna_space.max_depth.high", 8)
        => config["optuna_space"]["max_depth"]["high"] = 8
    """
    keys = path.split('.')
    current = config

    # Navigate to parent
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        elif not isinstance(current[key], dict):
            raise ValueError(f"Cannot traverse path '{path}': '{key}' is not a dict")
        current = current[key]

    # Set final value
    final_key = keys[-1]
    current[final_key] = value


def merge_list_change(base_list: list, change: Dict[str, Any]) -> list:
    """
    处理列表类型的变更

    change formats:
        {"action": "append", "value": "new_item"}
        {"action": "remove", "value": "item_to_remove"}
        {"action": "replace", "value": [...]}
    """
    action = change.get("action", "replace")
    value = change.get("value")

    if action == "append":
        return base_list + ([value] if not isinstance(value, list) else value)
    elif action == "remove":
        return [item for item in base_list if item != value]
    elif action == "replace":
        return value if isinstance(value, list) else [value]
    else:
        raise ValueError(f"Unknown list action: {action}")


def apply_patch(base_config: dict, patch: dict, verbose: bool = True) -> dict:
    """
    应用 AI Brain 输出的 patch 到基础配置

    Patch format:
    {
      "version": "v1",
      "reasoning": "...",
      "changes": {
        "optuna_space.max_depth.high": 8,
        "optuna.trials": 50
      }
    }
    """
    config = deepcopy(base_config)
    changes = patch.get("changes", {})

    if verbose:
        print(f"[Patch] Applying {len(changes)} changes...")
        if "reasoning" in patch:
            print(f"[Reasoning] {patch['reasoning']}\n")

    for path, value in changes.items():
        try:
            # Handle special list operations
            if isinstance(value, dict) and "action" in value:
                keys = path.split('.')
                current = config
                for key in keys[:-1]:
                    current = current[key]
                base_list = current.get(keys[-1], [])
                new_list = merge_list_change(base_list, value)
                current[keys[-1]] = new_list
                if verbose:
                    print(f"  ✓ {path}: {value['action']} -> {new_list}")
            else:
                apply_dot_path(config, path, value)
                if verbose:
                    print(f"  ✓ {path} = {value}")

        except Exception as e:
            print(f"  ✗ Failed to apply '{path}': {e}")
            raise

    return config


def validate_patch(patch: dict) -> bool:
    """验证 patch 格式"""
    if "changes" not in patch:
        raise ValueError("Patch missing 'changes' field")

    if not isinstance(patch["changes"], dict):
        raise ValueError("'changes' must be a dict")

    return True


def main():
    parser = argparse.ArgumentParser(description="Apply config patch from AI Brain")
    parser.add_argument(
        "--base", required=True, help="Path to base config JSON"
    )
    parser.add_argument(
        "--patch", required=True, help="Path to patch JSON from AI Brain"
    )
    parser.add_argument(
        "--output", required=True, help="Output config path"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show changes without writing"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress verbose output"
    )

    args = parser.parse_args()

    # Load base config
    print(f"[1/3] Loading base config from {args.base}...")
    with open(args.base, "r") as f:
        base_config = json.load(f)

    # Load patch
    print(f"[2/3] Loading patch from {args.patch}...")
    with open(args.patch, "r") as f:
        patch = json.load(f)

    validate_patch(patch)

    # Apply patch
    print(f"[3/3] Applying patch...")
    new_config = apply_patch(base_config, patch, verbose=not args.quiet)

    # Save or display
    if args.dry_run:
        print("\n[DRY RUN] Would write to:", args.output)
        print(json.dumps(new_config, indent=2, ensure_ascii=False))
    else:
        with open(args.output, "w") as f:
            json.dump(new_config, f, indent=2, ensure_ascii=False)
        print(f"\n✅ Config saved to: {args.output}")
        print(f"   Applied {len(patch.get('changes', {}))} changes")

        if "reasoning" in patch:
            print(f"\n📝 AI Brain Reasoning:\n{patch['reasoning']}")


if __name__ == "__main__":
    main()
