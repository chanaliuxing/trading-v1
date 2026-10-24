#!/usr/bin/env python3
"""
Safe Config Patcher - Auto-repair config.yml with auditing

Features:
- Dot-path set/merge with type checks
- Whitelist validation (only allow safe paths)
- Atomic write with backup rotation
- Audit logging

Usage:
    from scripts.ai_trader.config.config_patcher import update, get

    # Fix missing command
    result = update("commands.alpha_loop", "venv/bin/python alpha_loop.py ...", merge=True)

    # Read config value
    value = get("commands.alpha_loop")
"""
import os
import shutil
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from libs.logger import get_logger

logger = get_logger("ai_trader.config_patcher")

# Configuration file
CONFIG_FILE = Path("config.yml")
BACKUP_DIR = Path("backups/config")
MAX_BACKUPS = 5

# Whitelist of paths that can be auto-patched
ALLOWED_PATHS = {
    "commands.alpha_loop",
    "commands.backfill_yesterday",
    "commands.apply_params",
    "commands.rl_train",
    "commands.rl_eval",
    "commands.premarket_sh",
    "commands.first_hour_sh",
    "commands.all_providers_sh",
    "commands.premarket_fallback",
    "commands.first_hour_fallback",
    "commands.overnight_fallback",
    "policies.loop_sleep_sec.weekend",
    "policies.loop_sleep_sec.morning",
    "policies.loop_sleep_sec.intraday",
    "policies.loop_sleep_sec.afterhours",
    "policies.loop_sleep_sec.overnight",
    "policies.ml.slot",
    "policies.ml.quiet_hours_et",
    "policies.ml.priorities",
    "policies.ml.allow_pause",
}


def _is_path_allowed(path: str) -> bool:
    """Check if config path is in whitelist.

    Args:
        path: Dot-separated path (e.g., "commands.alpha_loop")

    Returns:
        True if path is whitelisted
    """
    # Exact match
    if path in ALLOWED_PATHS:
        return True

    # Prefix match for nested objects (e.g., "commands.*")
    for allowed in ALLOWED_PATHS:
        if path.startswith(allowed + "."):
            return True

    return False


def _create_backup() -> Path:
    """Create timestamped backup of config.yml.

    Returns:
        Path to backup file
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Create backup
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = BACKUP_DIR / f"config.yml.{timestamp}.yaml"

    shutil.copy2(CONFIG_FILE, backup_file)
    logger.info("config_backup_created", backup=str(backup_file))

    # Rotate old backups (keep last MAX_BACKUPS)
    backups = sorted(BACKUP_DIR.glob("config.yml.*.yaml"), key=lambda p: p.stat().st_mtime)
    if len(backups) > MAX_BACKUPS:
        for old_backup in backups[:-MAX_BACKUPS]:
            old_backup.unlink()
            logger.info("old_backup_removed", backup=str(old_backup))

    return backup_file


def _set_nested(data: Dict, path: str, value: Any, merge: bool = True) -> tuple[Any, Any]:
    """Set value at dot-separated path in nested dict.

    Args:
        data: Dictionary to modify
        path: Dot-separated path (e.g., "commands.alpha_loop")
        value: Value to set
        merge: If True and value is dict, merge with existing dict

    Returns:
        (old_value, new_value) tuple
    """
    keys = path.split(".")
    current = data

    # Navigate to parent
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        elif not isinstance(current[key], dict):
            raise ValueError(f"Path '{'.'.join(keys[:keys.index(key)+1])}' is not a dict")
        current = current[key]

    # Get old value
    final_key = keys[-1]
    old_value = current.get(final_key)

    # Set new value
    if merge and isinstance(value, dict) and isinstance(old_value, dict):
        # Merge dicts
        merged = {**old_value, **value}
        current[final_key] = merged
        new_value = merged
    else:
        # Direct replacement
        current[final_key] = value
        new_value = value

    return old_value, new_value


def get(path: str) -> Any:
    """Get config value at dot-separated path.

    Args:
        path: Dot-separated path (e.g., "commands.alpha_loop")

    Returns:
        Value at path, or None if not found
    """
    try:
        with CONFIG_FILE.open("r") as f:
            data = yaml.safe_load(f)

        keys = path.split(".")
        current = data

        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]

        return current

    except Exception as e:
        logger.error("config_read_failed", path=path, error=str(e))
        return None


def update(path: str, value: Any, merge: bool = True) -> Dict[str, Any]:
    """Update config value at dot-separated path with safety checks.

    Args:
        path: Dot-separated path (e.g., "commands.alpha_loop")
        value: Value to set
        merge: If True and value is dict, merge with existing

    Returns:
        Result dict with:
            - ok: bool
            - path: str
            - old_value: Any
            - new_value: Any
            - backup: str (if successful)
            - error: str (if failed)
            - merged: bool
    """
    result = {
        "ok": False,
        "path": path,
        "old_value": None,
        "new_value": None,
        "backup": None,
        "error": None,
        "merged": False
    }

    try:
        # 1. Whitelist check
        if not _is_path_allowed(path):
            result["error"] = f"Path '{path}' not in whitelist"
            logger.warning("config_patch_rejected", path=path, reason="not_whitelisted")
            return result

        # 2. Load current config
        with CONFIG_FILE.open("r") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            result["error"] = "Config root is not a dict"
            logger.error("config_patch_failed", reason="invalid_root")
            return result

        # 3. Create backup
        backup_file = _create_backup()
        result["backup"] = str(backup_file)

        # 4. Apply change
        old_value, new_value = _set_nested(data, path, value, merge=merge)
        result["old_value"] = old_value
        result["new_value"] = new_value
        result["merged"] = merge and isinstance(value, dict) and isinstance(old_value, dict)

        # 5. Atomic write
        tmp_file = CONFIG_FILE.with_suffix(".yml.tmp")

        with tmp_file.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # Replace original
        tmp_file.replace(CONFIG_FILE)

        result["ok"] = True
        logger.info("config_patch_applied",
                   path=path,
                   old=str(old_value)[:100],
                   new=str(new_value)[:100],
                   merged=result["merged"],
                   backup=str(backup_file))

        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error("config_patch_failed", path=path, error=str(e), exc_info=True)
        return result


def validate_command(command_name: str, command_value: str) -> Dict[str, Any]:
    """Validate command format.

    Args:
        command_name: Name of command (e.g., "alpha_loop")
        command_value: Command string

    Returns:
        Dict with:
            - valid: bool
            - issues: List[str]
    """
    issues = []

    # Basic checks
    if not command_value or not isinstance(command_value, str):
        issues.append("Command value is empty or not a string")
        return {"valid": False, "issues": issues}

    # Should start with executable
    if not (command_value.startswith("python") or
            command_value.startswith("venv/bin/python") or
            command_value.startswith("./")):
        issues.append("Command should start with python/venv/bin/python or ./")

    # Check for required flags per command type
    required_flags = {
        "alpha_loop": ["--provider", "--config"],
        "rl_train": ["--symbols", "--start", "--end"],
        "rl_eval": ["--symbols", "--start", "--end"],
    }

    if command_name in required_flags:
        for flag in required_flags[command_name]:
            if flag not in command_value:
                issues.append(f"Missing required flag: {flag}")

    return {
        "valid": len(issues) == 0,
        "issues": issues
    }


# Self-test
if __name__ == "__main__":
    print("Config Patcher Self-Test")
    print("=" * 60)

    # Test 1: Get
    print("\n[Test 1] Get existing value...")
    alpha_loop_cmd = get("commands.alpha_loop")
    print(f"  commands.alpha_loop: {alpha_loop_cmd}")

    # Test 2: Whitelist check
    print("\n[Test 2] Whitelist validation...")
    print(f"  'commands.alpha_loop' allowed: {_is_path_allowed('commands.alpha_loop')}")
    print(f"  'database.password' allowed: {_is_path_allowed('database.password')}")

    # Test 3: Validate command
    print("\n[Test 3] Command validation...")
    validation = validate_command("alpha_loop", alpha_loop_cmd or "")
    print(f"  Valid: {validation['valid']}")
    if validation['issues']:
        print(f"  Issues: {validation['issues']}")

    print("\n✅ Self-test complete")
