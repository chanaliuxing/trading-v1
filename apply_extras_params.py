#!/usr/bin/env python3
"""
Apply best_params (dot-notation) into config.yml strategies[*].params.extras

Usage:
  python apply_extras_params.py \
    --hpo-params alpha_out/daily/.../best_params.json \
    --config-yaml config.yml \
    --strategy-id enhanced_ml_001

Options:
  --no-backup          Skip backup creation (not recommended)

Environment:
  AI_TRADER_SKIP_CONFIG_BACKUP=1   Skip backup creation (not recommended)

Notes:
  - best_params should be a flat dict (supports dot notation for nesting)
  - only creates backup and writes if actual changes detected (skips if no diff)
  - backups saved to backups/config/config.yml.YYYYMMDD-HHMMSS.yaml
  - retention: use scripts/cleanup_old_config_backups.py --keep 10
"""

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml
try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore


def _merge_dot(dst: Dict[str, Any], dotted_key: str, val: Any) -> None:
    cur = dst
    parts = dotted_key.split(".")
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = val


def apply_params_to_config(best_params_path: str, config_yaml: str, strategy_id: str, skip_backup: bool = False) -> str:
    best = json.loads(Path(best_params_path).read_text("utf-8"))
    cfg_p = Path(config_yaml)
    # Acquire file lock if available
    if fcntl is not None:
        with open(cfg_p, "r+", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            data = yaml.safe_load(f) or {}
            # locate strategy by id
            strategies = data.get("strategies") or []
            target = None
            for s in strategies:
                if str(s.get("id")) == strategy_id:
                    target = s
                    break
            if target is None:
                raise RuntimeError(f"strategy id '{strategy_id}' not found in {config_yaml}")

            # Save original extras before modification
            original_extras = (((target.get("params") or {}).get("extras")) or {}).copy()

            extras = (((target.get("params") or {}).get("extras")) or {})
            # merge best params
            for k, v in (best or {}).items():
                if isinstance(k, str) and "." in k:
                    _merge_dot(extras, k, v)
                else:
                    extras[k] = v

            # Check if anything actually changed
            target.setdefault("params", {})["extras"] = extras
            new_yaml = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

            # Compare with current file content
            f.seek(0)
            current_yaml = f.read()

            if new_yaml == current_yaml:
                # No changes, skip backup and write
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                print(f"✓ No changes detected, skipping backup and write for {cfg_p}")
                return str(cfg_p)

            # Changes detected - backup to unified directory (unless skipped)
            if not skip_backup:
                bdir = Path("backups/config")
                bdir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                bak = bdir / f"config.yml.{ts}.yaml"
                try:
                    shutil.copy2(cfg_p, bak)
                    print(f"✓ Backup created: {bak}")
                except Exception as e:
                    print(f"⚠ Backup failed: {e}")
            else:
                print(f"⚠ Backup skipped (--no-backup or AI_TRADER_SKIP_CONFIG_BACKUP=1)")

            # Write changes
            f.seek(0)
            f.truncate()
            f.write(new_yaml)
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        return str(cfg_p)

    # Fallback path without fcntl lock (e.g., Windows)
    current_yaml = cfg_p.read_text()
    data = yaml.safe_load(current_yaml)

    # locate strategy by id
    strategies = data.get("strategies") or []
    target = None
    for s in strategies:
        if str(s.get("id")) == strategy_id:
            target = s
            break
    if target is None:
        raise RuntimeError(f"strategy id '{strategy_id}' not found in {config_yaml}")

    extras = (((target.get("params") or {}).get("extras")) or {})
    # merge best params
    for k, v in (best or {}).items():
        if isinstance(k, str) and "." in k:
            _merge_dot(extras, k, v)
        else:
            extras[k] = v

    # write back
    target.setdefault("params", {})["extras"] = extras

    # Check if anything changed
    new_yaml = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if new_yaml == current_yaml:
        print(f"✓ No changes detected, skipping backup and write for {cfg_p}")
        return str(cfg_p)

    # Changes detected - backup to unified directory (unless skipped)
    if not skip_backup:
        bdir = Path("backups/config")
        bdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = bdir / f"config.yml.{ts}.yaml"
        try:
            shutil.copy2(cfg_p, bak)
            print(f"✓ Backup created: {bak}")
        except Exception as e:
            print(f"⚠ Backup failed: {e}")
    else:
        print(f"⚠ Backup skipped (--no-backup or AI_TRADER_SKIP_CONFIG_BACKUP=1)")

    cfg_p.write_text(new_yaml, encoding="utf-8")
    return str(cfg_p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hpo-params", required=True)
    ap.add_argument("--config-yaml", required=True)
    ap.add_argument("--strategy-id", required=True)
    ap.add_argument("--no-backup", action="store_true", help="Skip backup creation (not recommended)")
    args = ap.parse_args()

    # Check environment variable
    skip_backup = args.no_backup or os.getenv("AI_TRADER_SKIP_CONFIG_BACKUP") == "1"

    out = apply_params_to_config(args.hpo_params, args.config_yaml, args.strategy_id, skip_backup=skip_backup)
    print(f"✓ params applied to {out}")


if __name__ == "__main__":
    main()
