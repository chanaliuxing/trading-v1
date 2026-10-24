from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple


VALID_SESSION_MODULES = {"baseline_rth_window", "ab_opening_window", "fast_drift_window"}
VALID_DIRECTION_MODULES = {"baseline_direction", "fast_drift_direction", "ab_direction", "elite_direction"}
VALID_GUARDS = {
    "amp_lock_guard",
    "stable_entry_guard",
    "restart_degrade_guard",
    "first_entry_seed_guard",
    "startup_anchor_guard",
    "open_orders_guard",
    "live_position_guard",
    "pending_decision_sync_guard",
    "ownership_guard",
    "qqq_freshness_guard",
    "qqew_freshness_guard",
    "continuity_180s_guard",
    "data_integrity_fail_closed_guard",
    "atr_ready_guard",
    "vwap_ready_guard",
    "volume_ready_guard",
}
VALID_OVERLAYS = {
    "extreme_overlay",
    "anti_chase_overlay",
    "reentry_cooldown_overlay",
    "high_vol_confirm_veto",
    "day_shape_overlay",
    "v47_chase_guard_overlay",
}
VALID_TRANSLATORS = {"qqq_option_translation", "tqqq_sqqq_translation"}


@dataclass(frozen=True)
class EntryRecipe:
    session_module: str
    direction_module: str
    guards: Tuple[str, ...]
    overlays: Tuple[str, ...]
    translator: str = "qqq_option_translation"


def _pick_default_direction(entry_model: str) -> str:
    model = str(entry_model or "baseline").strip().lower()
    if model == "v47_fast_drift":
        return "fast_drift_direction"
    if model == "v46_ab":
        return "ab_direction"
    if model == "v44_elite":
        return "elite_direction"
    return "baseline_direction"


def _pick_default_session(entry_model: str, extras: Dict[str, Any]) -> str:
    model = str(entry_model or "baseline").strip().lower()
    elite_ab_dual_mode = bool(extras.get("elite_ab_dual_mode", False))
    if model == "v47_fast_drift":
        return "fast_drift_window"
    if model in {"v46_ab"} or (model == "v44_elite" and elite_ab_dual_mode):
        return "ab_opening_window"
    return "baseline_rth_window"


def _as_tuple(raw: Any) -> Tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        return tuple(str(v).strip() for v in raw if str(v).strip())
    if isinstance(raw, str) and raw.strip():
        return (raw.strip(),)
    return tuple()


def _validate(values: Iterable[str], valid_set: set[str], field_name: str) -> None:
    invalid = sorted({v for v in values if v not in valid_set})
    if invalid:
        raise ValueError(f"invalid {field_name}: {', '.join(invalid)}")


def load_entry_recipe(extras: Dict[str, Any]) -> EntryRecipe:
    entry_model = str(extras.get("entry_model", "baseline")).strip().lower()
    raw_cfg = extras.get("entry_recipe")
    cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}

    session_module = str(cfg.get("session_module") or _pick_default_session(entry_model, extras)).strip()
    direction_module = str(cfg.get("direction_module") or _pick_default_direction(entry_model)).strip()
    guards = _as_tuple(cfg.get("guards")) or (
        "amp_lock_guard",
        "stable_entry_guard",
        "restart_degrade_guard",
        "first_entry_seed_guard",
        "startup_anchor_guard",
        "open_orders_guard",
        "live_position_guard",
        "pending_decision_sync_guard",
        "ownership_guard",
        "data_integrity_fail_closed_guard",
        "qqq_freshness_guard",
        "qqew_freshness_guard",
        "continuity_180s_guard",
        "atr_ready_guard",
        "vwap_ready_guard",
        "volume_ready_guard",
    )
    overlays = _as_tuple(cfg.get("overlays")) or (
        "extreme_overlay",
        "anti_chase_overlay",
        "v47_chase_guard_overlay",
        "reentry_cooldown_overlay",
        "high_vol_confirm_veto",
    )
    translator = str(cfg.get("translator") or "qqq_option_translation").strip()

    _validate((session_module,), VALID_SESSION_MODULES, "session_module")
    _validate((direction_module,), VALID_DIRECTION_MODULES, "direction_module")
    _validate(guards, VALID_GUARDS, "guards")
    _validate(overlays, VALID_OVERLAYS, "overlays")
    _validate((translator,), VALID_TRANSLATORS, "translator")
    return EntryRecipe(
        session_module=session_module,
        direction_module=direction_module,
        guards=tuple(guards),
        overlays=tuple(overlays),
        translator=translator,
    )


def load_stock_entry_recipe(extras: Dict[str, Any]) -> EntryRecipe:
    merged = dict(extras or {})
    raw_cfg = merged.get("entry_recipe")
    cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
    cfg.setdefault("translator", "tqqq_sqqq_translation")
    merged["entry_recipe"] = cfg
    return load_entry_recipe(merged)


def load_option_entry_recipe(extras: Dict[str, Any]) -> EntryRecipe:
    merged = dict(extras or {})
    raw_cfg = merged.get("entry_recipe")
    cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
    cfg.setdefault("translator", "qqq_option_translation")
    merged["entry_recipe"] = cfg
    return load_entry_recipe(merged)
