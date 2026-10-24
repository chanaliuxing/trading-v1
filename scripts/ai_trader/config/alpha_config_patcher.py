#!/usr/bin/env python3
"""
Alpha Config Patcher - Safe label_profile switching for alpha_config_*.json

Features:
- File whitelist (only alpha_config_enhanced*.json)
- Label profile whitelist (from label_profiles.yml, regression-only)
- Atomic write with backup
- Audit logging

Usage:
    from scripts.ai_trader.config.alpha_config_patcher import update, get

    # Switch label_profile for main alpha config
    result = update(
        file_path="alpha_config_enhanced.json",
        key="label_profile",
        value="intraday_reg_10m",
    )
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml  # type: ignore

from libs.logger import get_logger

logger = get_logger("ai_trader.alpha_config_patcher")

# Root directory (project root)
ROOT_DIR = Path(__file__).parent.parent.parent.parent

# Allowed alpha config JSON files (relative to ROOT_DIR)
ALLOWED_FILES = {
    "alpha_config_enhanced.json",
    "config/alpha_config_enhanced_debug.json",
}

# Backup settings
BACKUP_DIR = ROOT_DIR / "backups" / "alpha_config"
MAX_BACKUPS = 10


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
        # Outside root
        return False
    return rel in ALLOWED_FILES


def _load_allowed_label_profiles() -> set[str]:
    """Load regression label_profile names from label_profiles.yml."""
    path = ROOT_DIR / "label_profiles.yml"
    if not path.exists():
        logger.warning("label_profiles_not_found", path=str(path))
        return set()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.error("label_profiles_read_failed", path=str(path), error=str(e))
        return set()

    profiles = data.get("profiles") or {}
    allowed: set[str] = set()
    for name, cfg in profiles.items():
        try:
            if isinstance(cfg, dict) and (cfg.get("task") or "reg").lower() == "reg":
                allowed.add(str(name))
        except Exception:
            continue
    return allowed


def _create_backup(full_path: Path) -> Path:
    """Create timestamped backup of alpha_config JSON file."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = BACKUP_DIR / f"{full_path.name}.{ts}.json"
    shutil.copy2(full_path, backup_file)
    logger.info("alpha_config_backup_created", file=full_path.name, backup=str(backup_file))

    # Rotate old backups per file
    backups = sorted(
        BACKUP_DIR.glob(f"{full_path.name}.*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if len(backups) > MAX_BACKUPS:
        for old in backups[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)
            logger.info("old_alpha_config_backup_removed", backup=str(old))

    return backup_file


def get(file_path: str, key: str) -> Any:
    """Get value from alpha_config JSON (currently only label_profile supported)."""
    full = _normalize_path(file_path)
    if not full.exists():
        logger.error("alpha_config_file_not_found", file=file_path)
        return None
    try:
        data = json.loads(full.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("alpha_config_read_failed", file=file_path, error=str(e))
        return None

    if key == "label_profile":
        return data.get("label_profile")
    # Future keys could be added here
    logger.warning("alpha_config_key_not_supported", file=file_path, key=key)
    return None


def update(file_path: str, key: str, value: Any) -> Dict[str, Any]:
    """Update value in alpha_config JSON with safety checks (label_profile only)."""
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
            logger.warning("alpha_config_patch_rejected", file=file_path, reason="file_not_whitelisted")
            return result

        # Only support top-level label_profile for now
        if key != "label_profile":
            msg = f"Key '{key}' not supported for alpha_config patching"
            result["error"] = msg
            logger.warning("alpha_config_patch_rejected", file=file_path, key=key, reason="key_not_supported")
            return result

        if not isinstance(value, str):
            msg = f"label_profile value must be string, got {type(value).__name__}"
            result["error"] = msg
            logger.warning("alpha_config_patch_rejected", file=file_path, key=key, reason=msg)
            return result

        allowed = _load_allowed_label_profiles()
        if not allowed:
            msg = "No regression label profiles loaded from label_profiles.yml"
            result["error"] = msg
            logger.warning("alpha_config_patch_rejected", file=file_path, key=key, reason="no_allowed_profiles")
            return result

        if value not in allowed:
            msg = f"label_profile '{value}' not in allowed set {sorted(allowed)}"
            result["error"] = msg
            logger.warning("alpha_config_patch_rejected", file=file_path, key=key, reason="label_not_allowed")
            return result

        full = _normalize_path(file_path)
        if not full.exists():
            msg = f"File '{file_path}' does not exist"
            result["error"] = msg
            logger.error("alpha_config_file_not_found", file=file_path)
            return result

        data = json.loads(full.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = "Alpha config root is not a dict"
            result["error"] = msg
            logger.error("alpha_config_patch_failed", file=file_path, reason="invalid_root")
            return result

        backup_file = _create_backup(full)
        result["backup"] = str(backup_file)

        old_value = data.get("label_profile")
        data["label_profile"] = value
        result["old_value"] = old_value
        result["new_value"] = value

        tmp_file = full.with_suffix(full.suffix + ".tmp")
        tmp_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_file.replace(full)

        result["ok"] = True
        logger.info(
            "alpha_config_patch_applied",
            file=file_path,
            key=key,
            old=str(old_value),
            new=str(value),
            backup=str(backup_file),
        )
        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error("alpha_config_patch_failed", file=file_path, key=key, error=str(e), exc_info=True)
        return result

