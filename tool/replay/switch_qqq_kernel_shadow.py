#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
SHADOW_IDS = ("qqq_kernel_shadow_legacy_on_001", "qqq_kernel_shadow_legacy_off_001")


def _line_strategy_id(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("- id: "):
        return None
    return stripped.split(":", 1)[1].strip()


def switch_shadow_enabled(text: str, *, enabled: bool, strategy_ids: Iterable[str] = SHADOW_IDS) -> tuple[str, dict[str, bool]]:
    target_ids = set(strategy_ids)
    lines = text.splitlines()
    changed = {strategy_id: False for strategy_id in target_ids}
    current_id: str | None = None
    in_target = False
    target_indent: str | None = None
    replacement = "true" if enabled else "false"

    for idx, line in enumerate(lines):
        strategy_id = _line_strategy_id(line)
        if strategy_id is not None:
            current_id = strategy_id
            in_target = current_id in target_ids
            target_indent = None
            continue
        if not in_target or current_id is None:
            continue
        stripped = line.strip()
        if target_indent is None and stripped.startswith("enabled:"):
            target_indent = line[: len(line) - len(line.lstrip())]
            lines[idx] = f"{target_indent}enabled: {replacement}"
            changed[current_id] = True
            continue
        if target_indent is not None:
            continue

    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), changed


def _status(text: str, *, strategy_ids: Iterable[str] = SHADOW_IDS) -> dict[str, str]:
    target_ids = set(strategy_ids)
    out = {strategy_id: "missing" for strategy_id in target_ids}
    current_id: str | None = None
    in_target = False
    for line in text.splitlines():
        strategy_id = _line_strategy_id(line)
        if strategy_id is not None:
            current_id = strategy_id
            in_target = current_id in target_ids
            continue
        if in_target and current_id is not None and line.strip().startswith("enabled:"):
            out[current_id] = line.strip().split(":", 1)[1].strip()
            in_target = False
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enable/disable QQQ kernel shadow config blocks.")
    parser.add_argument("mode", choices=("status", "enable", "disable"))
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    args = parser.parse_args(argv)

    path = Path(args.config)
    text = path.read_text(encoding="utf-8")
    if args.mode == "status":
        for strategy_id, value in sorted(_status(text).items()):
            print(f"{strategy_id}.enabled={value}")
        return 0

    new_text, changed = switch_shadow_enabled(text, enabled=(args.mode == "enable"))
    missing = [strategy_id for strategy_id, ok in sorted(changed.items()) if not ok]
    if missing:
        print(f"missing_enabled_lines={','.join(missing)}")
        return 2
    path.write_text(new_text, encoding="utf-8")
    for strategy_id, value in sorted(_status(new_text).items()):
        print(f"{strategy_id}.enabled={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
