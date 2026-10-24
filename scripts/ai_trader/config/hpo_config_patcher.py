#!/usr/bin/env python3
"""
HPO Config Patcher - Safe edits for per-symbol HPO JSON configs.

Features:
- File whitelist (only config/hpo_tsla_working.json)
- Key whitelist (only objective.min_trades for now)
- Value boundary validation
- Atomic write with backup rotation
- Audit logging

Usage:
    from scripts.ai_trader.config.hpo_config_patcher import update, get

    # Example: relax or tighten min_trades requirement for TSLA HPO
    result = update(
        file_path="config/hpo_tsla_working.json",
        key="objective.min_trades",
        value=5,
    )
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

from libs.logger import get_logger

logger = get_logger("ai_trader.hpo_config_patcher")

# Root directory (project root)
ROOT_DIR = Path(__file__).parent.parent.parent.parent

# Allowed HPO config JSON files (relative to ROOT_DIR)
ALLOWED_FILES = {
    "config/hpo_tsla_working.json",
}

# Whitelisted keys and their value bounds
# Format: key_path -> (min_value, max_value, description)
PARAM_BOUNDS: Dict[str, Tuple[float, float, str]] = {
    "objective.min_trades": (0.0, 200.0, "Minimum trades required in HPO objective"),
}

# Backup settings
BACKUP_DIR = ROOT_DIR / "backups" / "hpo_config"
MAX_BACKUPS = 5


def _normalize_path(file_path: str) -> Path:
    """Normalize file path relative to project root."""
    p = Path(file_path)
    if p.is_absolute():
        return p
    return ROOT_DIR / p


def _is_file_allowed(file_path: str) -> bool:
    """Check if file is in whitelist."""
    full = _normalize_path(file_path)
    try:
        rel = str(full.relative_to(ROOT_DIR))
    except ValueError:
        return False
    return rel in ALLOWED_FILES


def _is_key_allowed(key: str) -> bool:
    """Check if key path is whitelisted."""
    return key in PARAM_BOUNDS


def _validate_value(key: str, value: Any) -> Tuple[bool, str]:
    """Validate value for a whitelisted key."""
    if key not in PARAM_BOUNDS:
        return False, f"Key '{key}' not in whitelist"
    min_val, max_val, desc = PARAM_BOUNDS[key]
    if not isinstance(value, (int, float)):
        return False, f"Value for {key} must be numeric, got {type(value).__name__}"
    if value < min_val or value > max_val:
        return False, f"{desc} must be in [{min_val}, {max_val}], got {value}"
    return True, ""


def _create_backup(full_path: Path) -> Path:
    """Create timestamped backup of HPO config JSON."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = BACKUP_DIR / f"{full_path.name}.{ts}.json"
    shutil.copy2(full_path, backup_file)
    logger.info("hpo_config_backup_created", file=full_path.name, backup=str(backup_file))

    # Rotate old backups per file
    backups = sorted(
        BACKUP_DIR.glob(f"{full_path.name}.*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if len(backups) > MAX_BACKUPS:
        for old in backups[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)
            logger.info("old_hpo_config_backup_removed", backup=str(old))

    return backup_file


def _get_nested(data: Dict[str, Any], key: str) -> Any:
    """Get value at dot-separated key path."""
    keys = key.split(".")
    cur: Any = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _set_nested(data: Dict[str, Any], key: str, value: Any) -> Tuple[Any, Any]:
    """Set value at dot-separated key path in nested dict."""
    keys = key.split(".")
    cur: Any = data
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    last = keys[-1]
    old = cur.get(last)
    cur[last] = value
    return old, value


def get(file_path: str, key: str) -> Any:
    """Get value from HPO config JSON (whitelisted keys only)."""
    full = _normalize_path(file_path)
    if not full.exists():
        logger.error("hpo_config_file_not_found", file=file_path)
        return None
    try:
        data = json.loads(full.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("hpo_config_read_failed", file=file_path, error=str(e))
        return None

    if not isinstance(data, dict):
        logger.error("hpo_config_invalid_root", file=file_path)
        return None

    if not _is_key_allowed(key):
        logger.warning("hpo_config_key_not_whitelisted", file=file_path, key=key)
        return None

    return _get_nested(data, key)


def update(file_path: str, key: str, value: Any) -> Dict[str, Any]:
    """Update value in HPO config JSON with safety checks (whitelist + bounds)."""
    result: Dict[str, Any] = {
        "ok": False,
        "file": file_path,
        "key": key,
        "old_value": None,
        "new_value": None,
        "backup": None,
        "error": None,
    }

    try:
        if not _is_file_allowed(file_path):
            msg = f"File '{file_path}' not in whitelist"
            result["error"] = msg
            logger.warning("hpo_config_patch_rejected", file=file_path, reason="file_not_whitelisted")
            return result

        if not _is_key_allowed(key):
            msg = f"Key '{key}' not whitelisted for HPO patching"
            result["error"] = msg
            logger.warning("hpo_config_patch_rejected", file=file_path, key=key, reason="key_not_whitelisted")
            return result

        is_valid, err = _validate_value(key, value)
        if not is_valid:
            result["error"] = err
            logger.warning("hpo_config_patch_rejected", file=file_path, key=key, reason=err)
            return result

        full = _normalize_path(file_path)
        if not full.exists():
            msg = f"File '{file_path}' does not exist"
            result["error"] = msg
            logger.error("hpo_config_file_not_found", file=file_path)
            return result

        data = json.loads(full.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = "HPO config root is not a dict"
            result["error"] = msg
            logger.error("hpo_config_patch_failed", file=file_path, reason="invalid_root")
            return result

        backup_file = _create_backup(full)
        result["backup"] = str(backup_file)

        old_value, new_value = _set_nested(data, key, value)
        result["old_value"] = old_value
        result["new_value"] = new_value

        tmp_file = full.with_suffix(full.suffix + ".tmp")
        tmp_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_file.replace(full)

        result["ok"] = True
        logger.info(
            "hpo_config_patch_applied",
            file=file_path,
            key=key,
            old=str(old_value),
            new=str(value),
            backup=str(backup_file),
        )
        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error("hpo_config_patch_failed", file=file_path, key=key, error=str(e), exc_info=True)
        return result

