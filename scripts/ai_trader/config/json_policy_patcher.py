#!/usr/bin/env python3
"""
JSON Policy Patcher - Safe threshold adjustment for ML policies

Features:
- File whitelist (only oof_policies.json, gating_policies.json)
- Path whitelist (only safe threshold paths)
- Value boundary validation (prevent absurd values)
- Atomic write with backup
- Audit logging

Usage:
    from scripts.ai_trader.config.json_policy_patcher import update, get

    # Adjust AUC threshold
    result = update(
        file_path="config/oof_policies.json",
        key="defaults.is_days",
        value=90
    )
"""
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from libs.logger import get_logger

logger = get_logger("ai_trader.json_policy_patcher")

# Root directory for all paths
ROOT_DIR = Path(__file__).parent.parent.parent.parent

# File whitelist - only these JSON files can be modified
ALLOWED_FILES = {
    "config/oof_policies.json",
    "config/gating_policies.json",
    "ml_policy.json",
    # Per-symbol HPO policy (strictly bounded keys only)
    "config/hpo_tsla_working.json",
    # Feature catalog tuning (restricted to specific keys below)
    "config/features_catalog.json",
    # Feature family toggles (restricted to families.*.enabled)
    "config/feature_families.json",
}

# Path + value boundaries for safe threshold adjustment
# Format: (min_value, max_value, description)
PARAM_BOUNDS = {
    "config/oof_policies.json": {
        "defaults.is_days": (30, 180, "In-sample training days"),
        "defaults.oos_days": (1, 30, "Out-of-sample validation days"),
        "defaults.step_days": (1, 7, "Rolling window step size"),
        "defaults.horizon_min": (5, 15, "Prediction horizon in minutes"),
        "defaults.label_bps": (5, 30, "Target return in basis points"),
        "defaults.min_is_rows": (50, 500, "Minimum in-sample rows"),
        "defaults.exclude_open_min": (0, 30, "Minutes to exclude after open"),
        "defaults.xgb.n_estimators": (50, 500, "XGB tree count"),
        "defaults.xgb.max_depth": (2, 6, "XGB max tree depth"),
        "defaults.xgb.learning_rate": (0.01, 0.2, "XGB learning rate"),
        "defaults.xgb.min_child_weight": (1, 20, "XGB min child weight"),
        "defaults.xgb.subsample": (0.5, 1.0, "XGB row sampling ratio"),
        "defaults.xgb.colsample_bytree": (0.5, 1.0, "XGB column sampling ratio"),
        "defaults.xgb.reg_lambda": (0.1, 10.0, "XGB L2 regularization"),
        "models.xgb.horizon_min": (5, 15, "XGB-specific horizon"),
        "models.lstm.min_is_rows": (30, 300, "LSTM min training rows"),
    },
    "config/gating_policies.json": {
        "defaults.conf_floor": (0.0, 0.95, "Default confidence floor"),
        # Symbol-specific paths use wildcard matching
        # e.g., "symbols.AAPL.conf_floor" matches "symbols.*.conf_floor"
        "symbols.*.conf_floor": (0.0, 0.95, "Symbol-specific confidence floor"),
    },
    "ml_policy.json": {
        "label.horizon_sec": (30, 600, "Label horizon in seconds"),
        "label.clip_abs": (0.01, 0.5, "Label log-return clip abs"),
        "weights.power": (0.5, 3.0, "Volatility-based sample weight power"),
    },
    "config/hpo_tsla_working.json": {
        "objective.min_trades": (1, 30, "Minimum trades constraint for TSLA HPO"),
    },
    "config/feature_families.json": {
        # Families.*.enabled is boolean, but we treat as [0,1] numeric bounds
        "families.*.enabled": (0.0, 1.0, "Feature family on/off toggle"),
    },
}

# Backup settings
BACKUP_DIR = ROOT_DIR / "backups" / "policies"
MAX_BACKUPS = 10


def _normalize_path(file_path: str) -> Path:
    """Normalize file path relative to project root.

    Args:
        file_path: Relative path (e.g., "config/oof_policies.json")

    Returns:
        Absolute Path object
    """
    if file_path.startswith("/"):
        return Path(file_path)
    return ROOT_DIR / file_path


def _is_file_allowed(file_path: str) -> bool:
    """Check if file is in whitelist.

    Args:
        file_path: File path to check

    Returns:
        True if file is whitelisted
    """
    # Normalize to relative path for comparison
    normalized = str(_normalize_path(file_path).relative_to(ROOT_DIR))
    return normalized in ALLOWED_FILES


def _match_wildcard_path(pattern: str, path: str) -> bool:
    """Check if path matches wildcard pattern.

    Args:
        pattern: Pattern with * wildcards (e.g., "symbols.*.conf_floor")
        path: Actual path (e.g., "symbols.AAPL.conf_floor")

    Returns:
        True if path matches pattern
    """
    pattern_parts = pattern.split(".")
    path_parts = path.split(".")

    if len(pattern_parts) != len(path_parts):
        return False

    for p_part, path_part in zip(pattern_parts, path_parts):
        if p_part != "*" and p_part != path_part:
            return False

    return True


def _get_bounds(file_path: str, key: str) -> Optional[Tuple[float, float, str]]:
    """Get value boundaries for a key.

    Args:
        file_path: JSON file path
        key: Dot-separated key path

    Returns:
        (min, max, description) tuple or None if not found
    """
    normalized = str(_normalize_path(file_path).relative_to(ROOT_DIR))

    if normalized not in PARAM_BOUNDS:
        return None

    bounds_dict = PARAM_BOUNDS[normalized]

    # Try exact match first
    if key in bounds_dict:
        return bounds_dict[key]

    # Try wildcard match (e.g., "symbols.*.conf_floor")
    for pattern, bounds in bounds_dict.items():
        if "*" in pattern and _match_wildcard_path(pattern, key):
            return bounds

    return None


def _validate_value(file_path: str, key: str, value: Any) -> Tuple[bool, str]:
    """Validate value is within safe boundaries.

    Args:
        file_path: JSON file path
        key: Dot-separated key path
        value: Value to validate

    Returns:
        (is_valid, error_message) tuple
    """
    # Special-case: feature catalog expression tweaks
    if Path(file_path).name == "features_catalog.json" and str(key).endswith(".expr"):
        if not isinstance(value, str):
            return False, f"Value must be string for {file_path}:{key}"
        if len(value) > 1000:
            return False, "Expression too long (>1000 chars)"
        # Guard against known-unsafe SQL fragments that have repeatedly
        # broken feature_build (e.g. standard SQL window frames that are not
        # compatible with our ClickHouse version / settings).
        v_lower = value.lower()
        banned_fragments = (
            "rows between",
            "stddevpop(",
            "stddevsamp(",
            "movingaverage(",
        )
        if any(bad in v_lower for bad in banned_fragments):
            return False, "Expression contains unsupported SQL window syntax (ROWS BETWEEN / stddevPop / stddevSamp)"
        return True, ""

    bounds = _get_bounds(file_path, key)

    if bounds is None:
        return False, f"Key '{key}' not in whitelist for {file_path}"

    min_val, max_val, description = bounds

    # Type check
    if not isinstance(value, (int, float)):
        return False, f"Value must be numeric, got {type(value).__name__}"

    # Range check
    if value < min_val or value > max_val:
        return False, f"{description} must be in [{min_val}, {max_val}], got {value}"

    return True, ""


def _create_backup(file_path: Path) -> Path:
    """Create timestamped backup of JSON file.

    Args:
        file_path: Path to JSON file

    Returns:
        Path to backup file
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Create backup
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = BACKUP_DIR / f"{file_path.stem}.{timestamp}.json"

    shutil.copy2(file_path, backup_file)
    logger.info("policy_backup_created", file=file_path.name, backup=str(backup_file))

    # Rotate old backups (keep last MAX_BACKUPS per file)
    backups = sorted(
        BACKUP_DIR.glob(f"{file_path.stem}.*.json"),
        key=lambda p: p.stat().st_mtime
    )
    if len(backups) > MAX_BACKUPS:
        for old_backup in backups[:-MAX_BACKUPS]:
            old_backup.unlink()
            logger.info("old_policy_backup_removed", backup=str(old_backup))

    return backup_file


def _set_nested(data: Dict, key: str, value: Any) -> Tuple[Any, Any]:
    """Set value at dot-separated key path in nested dict.

    Args:
        data: Dictionary to modify
        key: Dot-separated key path (e.g., "defaults.is_days")
        value: Value to set

    Returns:
        (old_value, new_value) tuple
    """
    keys = key.split(".")
    current = data

    # Navigate to parent
    for k in keys[:-1]:
        if k not in current:
            current[k] = {}
        elif not isinstance(current[k], dict):
            raise ValueError(f"Path '{k}' is not a dict")
        current = current[k]

    # Set value
    final_key = keys[-1]
    old_value = current.get(final_key)
    current[final_key] = value

    return old_value, value


def _get_nested(data: Dict, key: str) -> Any:
    """Get value at dot-separated key path.

    Args:
        data: Dictionary to read from
        key: Dot-separated key path

    Returns:
        Value at key, or None if not found
    """
    keys = key.split(".")
    current = data

    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return None
        current = current[k]

    return current


def get(file_path: str, key: str) -> Any:
    """Get value from JSON policy file.

    Args:
        file_path: JSON file path (e.g., "config/oof_policies.json")
        key: Dot-separated key path (e.g., "defaults.is_days")

    Returns:
        Value at key, or None if not found
    """
    try:
        full_path = _normalize_path(file_path)

        if not full_path.exists():
            logger.error("policy_file_not_found", file=file_path)
            return None

        with full_path.open("r") as f:
            data = json.load(f)

        return _get_nested(data, key)

    except Exception as e:
        logger.error("policy_read_failed", file=file_path, key=key, error=str(e))
        return None


def update(file_path: str, key: str, value: Any) -> Dict[str, Any]:
    """Update value in JSON policy file with safety checks.

    Args:
        file_path: JSON file path (e.g., "config/oof_policies.json")
        key: Dot-separated key path (e.g., "defaults.is_days")
        value: Value to set

    Returns:
        Result dict with:
            - ok: bool
            - file: str
            - key: str
            - old_value: Any
            - new_value: Any
            - backup: str (if successful)
            - error: str (if failed)
    """
    result = {
        "ok": False,
        "file": file_path,
        "key": key,
        "old_value": None,
        "new_value": None,
        "backup": None,
        "error": None
    }

    try:
        # 1. File whitelist check
        if not _is_file_allowed(file_path):
            result["error"] = f"File '{file_path}' not in whitelist"
            logger.warning("policy_patch_rejected", file=file_path, reason="file_not_whitelisted")
            return result

        # 2. Value validation (path whitelist + boundary check)
        is_valid, error_msg = _validate_value(file_path, key, value)
        if not is_valid:
            result["error"] = error_msg
            logger.warning("policy_patch_rejected", file=file_path, key=key, reason=error_msg)
            return result

        # 3. Load current policy
        full_path = _normalize_path(file_path)

        if not full_path.exists():
            result["error"] = f"File '{file_path}' does not exist"
            logger.error("policy_file_not_found", file=file_path)
            return result

        with full_path.open("r") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            result["error"] = "Policy root is not a dict"
            logger.error("policy_patch_failed", file=file_path, reason="invalid_root")
            return result

        # 3a. Profit-first guard: prevent lowering conf_floor
        try:
            normalized = str(full_path.relative_to(ROOT_DIR))
        except Exception:
            normalized = ""
        if normalized == "config/gating_policies.json" and key.endswith("conf_floor"):
            try:
                current_val = _get_nested(data, key)
            except Exception:
                current_val = None
            if isinstance(current_val, (int, float)) and isinstance(value, (int, float)) and value < current_val:
                msg = f"conf_floor cannot be lowered (current={current_val}, requested={value})"
                result["error"] = msg
                logger.warning(
                    "policy_patch_rejected",
                    file=file_path,
                    key=key,
                    reason="conf_floor_lowering_not_allowed",
                    current=current_val,
                    requested=value,
                )
                return result

        # 4. Create backup
        backup_file = _create_backup(full_path)
        result["backup"] = str(backup_file)

        # 5. Apply change
        old_value, new_value = _set_nested(data, key, value)
        result["old_value"] = old_value
        result["new_value"] = new_value

        # 6. Atomic write
        tmp_file = full_path.with_suffix(".json.tmp")

        with tmp_file.open("w") as f:
            json.dump(data, f, indent=2)

        # Replace original
        tmp_file.replace(full_path)

        result["ok"] = True
        logger.info("policy_patch_applied",
                   file=file_path,
                   key=key,
                   old=str(old_value),
                   new=str(new_value),
                   backup=str(backup_file))

        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error("policy_patch_failed", file=file_path, key=key, error=str(e), exc_info=True)
        return result


# Self-test
if __name__ == "__main__":
    print("JSON Policy Patcher Self-Test")
    print("=" * 60)

    # Test 1: File whitelist
    print("\n[Test 1] File whitelist validation...")
    print(f"  config/oof_policies.json allowed: {_is_file_allowed('config/oof_policies.json')}")
    print(f"  config/gating_policies.json allowed: {_is_file_allowed('config/gating_policies.json')}")
    print(f"  config/random.json allowed: {_is_file_allowed('config/random.json')}")

    # Test 2: Bounds checking
    print("\n[Test 2] Bounds validation...")
    bounds = _get_bounds("config/oof_policies.json", "defaults.is_days")
    print(f"  defaults.is_days bounds: {bounds}")

    is_valid, msg = _validate_value("config/oof_policies.json", "defaults.is_days", 90)
    print(f"  is_days=90 valid: {is_valid}")

    is_valid, msg = _validate_value("config/oof_policies.json", "defaults.is_days", 500)
    print(f"  is_days=500 valid: {is_valid} (error: {msg})")

    # Test 3: Wildcard path matching
    print("\n[Test 3] Wildcard path matching...")
    pattern = "symbols.*.conf_floor"
    print(f"  Pattern: {pattern}")
    print(f"  Matches 'symbols.AAPL.conf_floor': {_match_wildcard_path(pattern, 'symbols.AAPL.conf_floor')}")
    print(f"  Matches 'symbols.TSLA.conf_floor': {_match_wildcard_path(pattern, 'symbols.TSLA.conf_floor')}")
    print(f"  Matches 'defaults.conf_floor': {_match_wildcard_path(pattern, 'defaults.conf_floor')}")

    # Test 4: Get value
    print("\n[Test 4] Get value...")
    value = get("config/oof_policies.json", "defaults.is_days")
    print(f"  defaults.is_days = {value}")

    print("\n✅ Self-test complete")
