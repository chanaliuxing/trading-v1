from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class ReplicaEntryGateDecision:
    allowed: bool
    reason: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


def _feature_prefix(symbol: Any) -> str:
    return str(symbol or "").strip().lower().replace("-", "_")


def _gate_symbol_config(extras: Dict[str, Any]) -> Dict[str, str]:
    primary_symbol = str(extras.get("gate_primary_symbol") or extras.get("signal_symbol") or "QQQ").strip().upper() or "QQQ"
    confirm_symbol = str(extras.get("gate_confirm_symbol") or extras.get("confirm_symbol") or primary_symbol).strip().upper() or primary_symbol
    primary_prefix = str(extras.get("gate_primary_feature_prefix") or _feature_prefix(primary_symbol)).strip().lower() or "qqq"
    confirm_prefix = str(extras.get("gate_confirm_feature_prefix") or _feature_prefix(confirm_symbol)).strip().lower() or primary_prefix
    return {
        "primary_symbol": primary_symbol,
        "confirm_symbol": confirm_symbol,
        "primary_prefix": primary_prefix,
        "confirm_prefix": confirm_prefix,
    }


def _feature_value(features: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key and key in features:
            return features.get(key)
    return None


def _normalize_missing_required(raw_items: Any) -> List[str]:
    if not isinstance(raw_items, Iterable) or isinstance(raw_items, (str, bytes, dict)):
        return []
    normalized: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            name = str(item.get("symbol") or item.get("name") or item.get("field") or "").strip()
        else:
            name = str(item or "").strip()
        if not name:
            continue
        normalized.append(name.upper())
    # preserve order while deduping
    return list(dict.fromkeys(normalized))


def _first_freshness_breach(features: Dict[str, Any], extras: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    config = _gate_symbol_config(extras)
    pairs: list[tuple[str, str]] = [(config["primary_symbol"], config["primary_prefix"])]
    if bool(extras.get("use_confirm_symbol", True)):
        pairs.append((config["confirm_symbol"], config["confirm_prefix"]))
    for symbol_u, key in pairs:
        age = _feature_value(features, f"{key}_age_sec", f"freshness_age_sec_{key}")
        if age is None:
            age = _feature_value(features, f"{_feature_prefix(symbol_u)}_age_sec", f"freshness_age_sec_{_feature_prefix(symbol_u)}")
        limit = features.get(f"{key}_freshness_limit_sec")
        if limit is None:
            limit = extras.get(f"freshness_max_sec_{key}", extras.get("freshness_max_sec", 5.0))
        try:
            age_f = float(age)
            limit_f = float(limit)
        except (TypeError, ValueError):
            continue
        if age_f > limit_f:
            return {"symbol": symbol_u, "age_sec": age_f, "limit_sec": limit_f}
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_time(value: Any) -> Optional[time]:
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.timetz().replace(tzinfo=None)
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def _feature_now_time(features: Dict[str, Any]) -> Optional[time]:
    for key in ("now_et", "now_time_et", "feature_time_et"):
        parsed = _safe_time(features.get(key))
        if parsed is not None:
            return parsed
    return None


def _entry_data_integrity_failure(features: Dict[str, Any], extras: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    config = _gate_symbol_config(extras)
    max_data_age_sec = _safe_float(extras.get("entry_max_data_age_sec")) or 3.0
    primary_prefix = config["primary_prefix"]
    confirm_prefix = config["confirm_prefix"]
    primary_symbol = config["primary_symbol"]
    confirm_symbol = config["confirm_symbol"]
    max_data_age_primary_sec = _safe_float(extras.get(f"entry_max_data_age_sec_{primary_prefix}")) or max_data_age_sec
    primary_age = _safe_float(_feature_value(features, f"{primary_prefix}_age_sec", f"freshness_age_sec_{primary_prefix}"))
    if primary_age is None:
        return {"kind": "freshness_field_missing", "symbol": primary_symbol, "field": f"{primary_prefix}_age_sec", "limit_sec": max_data_age_primary_sec}
    if primary_age > max_data_age_primary_sec:
        return {"kind": "stale", "symbol": primary_symbol, "age_sec": primary_age, "limit_sec": max_data_age_primary_sec}

    qqew_decoupled = bool(_feature_value(features, f"{confirm_prefix}_decoupled", "qqew_decoupled", "confirm_symbol_decoupled", "confirm_decoupled"))
    use_confirm_symbol = bool(extras.get("use_confirm_symbol", False))
    if use_confirm_symbol and not qqew_decoupled:
        max_data_age_confirm_sec = _safe_float(extras.get(f"entry_max_data_age_sec_{confirm_prefix}")) or max_data_age_sec
        confirm_age = _safe_float(_feature_value(features, f"{confirm_prefix}_age_sec", f"freshness_age_sec_{confirm_prefix}"))
        if confirm_age is None:
            return {"kind": "freshness_field_missing", "symbol": confirm_symbol, "field": f"{confirm_prefix}_age_sec", "limit_sec": max_data_age_confirm_sec}
        if confirm_age > max_data_age_confirm_sec:
            return {"kind": "stale", "symbol": confirm_symbol, "age_sec": confirm_age, "limit_sec": max_data_age_confirm_sec}

    if bool(extras.get("entry_require_continuity_180s", extras.get("require_continuity_180s", False))) and not bool(
        features.get("continuous_180s", True)
    ):
        return {"kind": "continuity", "symbol": primary_symbol}

    now_t = _feature_now_time(features)
    if now_t is not None and now_t < time(10, 0):
        src = str(features.get("price_base_source") or "").strip().lower()
        if src in {"", "unknown"}:
            return {"kind": "price_base_source_unknown", "source": src or "unknown"}

    return None


def evaluate_replica_entry_gate(
    *,
    features: Dict[str, Any],
    extras: Dict[str, Any],
) -> ReplicaEntryGateDecision:
    config = _gate_symbol_config(extras)
    primary_symbol = config["primary_symbol"]
    confirm_symbol = config["confirm_symbol"]
    primary_prefix = config["primary_prefix"]
    confirm_prefix = config["confirm_prefix"]
    if not bool(extras.get("stable_entry_guard_enabled", True)):
        return ReplicaEntryGateDecision(True, details={"stable_entry_guard_enabled": False})

    now_t = _feature_now_time(features)
    ab_start = _safe_time(extras.get("ab_time_start"))
    ab_end = _safe_time(extras.get("ab_time_end"))
    if now_t is not None and ab_start is not None and ab_end is not None:
        if now_t < ab_start or now_t > ab_end:
            return ReplicaEntryGateDecision(
                False,
                "outside_ab_window",
                {"now_time_et": now_t.isoformat(), "ab_time_start": ab_start.isoformat(), "ab_time_end": ab_end.isoformat()},
            )
    no_new_after = _safe_time(extras.get("no_new_entry_after"))
    if now_t is not None and no_new_after is not None:
        if now_t >= no_new_after:
            return ReplicaEntryGateDecision(
                False,
                "after_no_new_entry",
                {"now_time_et": now_t.isoformat(), "no_new_entry_after": no_new_after.isoformat()},
            )

    if bool(extras.get("entry_require_decision_pipeline_healthy", True)) and not bool(features.get("decision_pipeline_healthy", True)):
        return ReplicaEntryGateDecision(False, "decision_pipeline_unavailable")

    if bool(extras.get("entry_require_market_data_healthy", True)) and not bool(features.get("market_data_healthy", True)):
        return ReplicaEntryGateDecision(False, "market_data_unavailable")

    if bool(extras.get("fail_closed_on_data_integrity", False)):
        integrity_failure = _entry_data_integrity_failure(features, extras)
        if integrity_failure is not None:
            return ReplicaEntryGateDecision(False, "data_integrity_fail_closed", {"integrity_failure": integrity_failure})
        if not bool(features.get("data_integrity_ready", True)):
            return ReplicaEntryGateDecision(False, "data_integrity_fail_closed")

    if bool(extras.get("first_entry_require_seed_ready", False)) and bool(features.get("first_entry_guard_context", False)):
        min_samples = int(extras.get("entry_seed_min_samples", 20) or 20)
        sample_count = int(features.get("seed_sample_count", 0) or 0)
        if (not bool(features.get("seed_ready", False))) or sample_count < min_samples:
            return ReplicaEntryGateDecision(
                False,
                "first_entry_seed_not_ready",
                {"seed_sample_count": sample_count, "required_min_samples": min_samples},
            )

    embargo_enabled = bool(features.get("startup_anchor_embargo", False)) or bool(extras.get("startup_anchor_entry_block_enabled", False))
    if embargo_enabled:
        embargo_start = _safe_time(extras.get("startup_anchor_entry_block_start")) or time(9, 30)
        embargo_end = _safe_time(extras.get("startup_anchor_entry_block_end")) or time(10, 0)
        now_t = _feature_now_time(features)
        src = str(features.get("price_base_source") or "").strip().lower()
        if bool(features.get("startup_anchor_embargo", False)) or (
            now_t is not None and src == "startup_anchor_10s" and embargo_start <= now_t < embargo_end
        ):
            return ReplicaEntryGateDecision(
                False,
                "startup_anchor_embargo",
                {"price_base_source": src or None, "embargo_start": embargo_start.isoformat(), "embargo_end": embargo_end.isoformat()},
            )

    if bool(features.get("restart_degrade_active", False)):
        return ReplicaEntryGateDecision(False, "restart_degrade_active")

    missing_required = features.get("required_data_missing")
    if missing_required is None:
        missing_required = features.get("missing_required")
    normalized_missing = _normalize_missing_required(missing_required or [])
    if normalized_missing:
        return ReplicaEntryGateDecision(False, "required_data_missing", {"missing_required": normalized_missing})

    freshness_breach = _first_freshness_breach(features, extras)
    if freshness_breach is not None:
        return ReplicaEntryGateDecision(False, "data_not_ready", {"freshness_breach": freshness_breach})

    if bool(extras.get("require_continuity_180s", False)) and not bool(features.get("continuous_180s", True)):
        return ReplicaEntryGateDecision(False, "data_not_ready", {"continuity": "missing_180s"})

    if not bool(features.get("data_ready", True)):
        return ReplicaEntryGateDecision(False, "data_not_ready")

    side = str(features.get("intent_side") or features.get("side") or "").strip().upper()
    primary_now = _feature_value(features, f"{primary_prefix}_close_now", "qqq_close_now", "signal_price")
    vwap_now = _feature_value(features, f"{primary_prefix}_vwap_now", "vwap_now", "qqq_vwap")
    primary_now_f = _safe_float(primary_now)
    vwap_now_f = _safe_float(vwap_now)
    if bool(extras.get("require_vwap_ready", False)) and side in {"LONG", "SHORT"} and (vwap_now_f is None or vwap_now_f <= 0):
        return ReplicaEntryGateDecision(False, "data_not_ready", {"missing": f"{primary_symbol}:VWAP"})

    qqew_decoupled = bool(_feature_value(features, f"{confirm_prefix}_decoupled", "qqew_decoupled", "confirm_symbol_decoupled", "confirm_decoupled"))
    if bool(extras.get("require_confirm_vwap_ready", False)) and bool(extras.get("use_confirm_symbol", False)) and not qqew_decoupled:
        confirm_vwap_now_f = _safe_float(_feature_value(features, f"{confirm_prefix}_vwap_now", "qqew_vwap_now"))
        if confirm_vwap_now_f is None or confirm_vwap_now_f <= 0:
            return ReplicaEntryGateDecision(False, "data_not_ready", {"missing": f"{confirm_symbol}:VWAP"})

    if bool(extras.get("require_atr_ready", False)):
        atr_live_f = _safe_float(features.get("atr_live"))
        atr_seed_f = _safe_float(features.get("atr_seed"))
        atr_cap_f = _safe_float(features.get("atr_cap"))
        if not any(value is not None and value > 0 for value in (atr_live_f, atr_seed_f, atr_cap_f)):
            return ReplicaEntryGateDecision(False, "data_not_ready", {"missing": "ATR"})

    distance_from_vwap_atr = _safe_float(features.get("distance_from_vwap_atr"))
    extreme_dist_min = _safe_float(extras.get("entry_extreme_distance_from_vwap_atr_min"))
    if bool(extras.get("entry_extreme_guard_enabled", False)) and distance_from_vwap_atr is not None and extreme_dist_min is not None:
        if distance_from_vwap_atr > extreme_dist_min:
            return ReplicaEntryGateDecision(
                False,
                "extreme_blocked",
                {"distance_from_vwap_atr": distance_from_vwap_atr, "threshold": extreme_dist_min},
            )
    if side in {"LONG", "SHORT"} and primary_now_f is not None and vwap_now_f is not None and vwap_now_f > 0:
        if side == "LONG" and not (primary_now_f > vwap_now_f):
            return ReplicaEntryGateDecision(
                False,
                "direction_mismatch",
                {"direction": side, f"{primary_prefix}_close_now": primary_now_f, "vwap_now": vwap_now_f},
            )
        if side == "SHORT" and not (primary_now_f < vwap_now_f):
            return ReplicaEntryGateDecision(
                False,
                "direction_mismatch",
                {"direction": side, f"{primary_prefix}_close_now": primary_now_f, "vwap_now": vwap_now_f},
            )

    if bool(extras.get("use_confirm_symbol", False)) and side in {"LONG", "SHORT"} and not qqew_decoupled:
        confirm_now = _feature_value(features, f"{confirm_prefix}_close_now", "qqew_close_now")
        confirm_vwap_now = _feature_value(features, f"{confirm_prefix}_vwap_now", "qqew_vwap_now")
        confirm_now_f = _safe_float(confirm_now)
        confirm_vwap_now_f = _safe_float(confirm_vwap_now)
        if confirm_now_f is not None and confirm_vwap_now_f is not None and confirm_vwap_now_f > 0:
            if side == "LONG" and not (confirm_now_f > confirm_vwap_now_f):
                return ReplicaEntryGateDecision(
                    False,
                    "direction_mismatch",
                    {"direction": side, f"{confirm_prefix}_close_now": confirm_now_f, f"{confirm_prefix}_vwap_now": confirm_vwap_now_f},
                )
            if side == "SHORT" and not (confirm_now_f < confirm_vwap_now_f):
                return ReplicaEntryGateDecision(
                    False,
                    "direction_mismatch",
                    {"direction": side, f"{confirm_prefix}_close_now": confirm_now_f, f"{confirm_prefix}_vwap_now": confirm_vwap_now_f},
                )

    return ReplicaEntryGateDecision(True)
