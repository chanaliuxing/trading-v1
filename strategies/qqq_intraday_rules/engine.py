from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from .contracts import DirectionDecision, EntryContext, RuleDecision, TranslationDecision
from .direction import ab_direction, baseline_direction, elite_direction, fast_drift_direction
from .guards import (
    amp_lock_guard,
    atr_ready_guard,
    continuity_180s_guard,
    data_integrity_fail_closed_guard,
    live_position_guard,
    open_orders_guard,
    ownership_guard,
    first_entry_seed_guard,
    pending_decision_sync_guard,
    qqq_freshness_guard,
    qqew_freshness_guard,
    restart_degrade_guard,
    session_window_guard,
    stable_entry_guard,
    startup_anchor_guard,
    volume_ready_guard,
    vwap_ready_guard,
)
from .overlays import (
    anti_chase_overlay,
    day_shape_overlay,
    high_vol_confirm_veto,
    reentry_cooldown_overlay,
    extreme_overlay,
    v47_chase_guard_overlay,
)
from .recipe import EntryRecipe
from .runtime_context import build_entry_context
from .translator import qqq_option_translation, tqqq_sqqq_translation


GuardFn = Callable[[EntryContext], RuleDecision]
DirectionFn = Callable[[EntryContext], DirectionDecision]
OverlayFn = Callable[[EntryContext, DirectionDecision], DirectionDecision]
TranslatorFn = Callable[[EntryContext, DirectionDecision], TranslationDecision]


@dataclass(frozen=True)
class EntryEvaluation:
    translation: TranslationDecision
    context: EntryContext
    gate_reason: str
    gate_payload: Dict[str, object]


class QQQIntradayRuleEngine:
    def __init__(self) -> None:
        self._guards: Dict[str, GuardFn] = {
            "amp_lock_guard": amp_lock_guard,
            "stable_entry_guard": stable_entry_guard,
            "restart_degrade_guard": restart_degrade_guard,
            "first_entry_seed_guard": first_entry_seed_guard,
            "startup_anchor_guard": startup_anchor_guard,
            "open_orders_guard": open_orders_guard,
            "live_position_guard": live_position_guard,
            "pending_decision_sync_guard": pending_decision_sync_guard,
            "ownership_guard": ownership_guard,
            "qqq_freshness_guard": qqq_freshness_guard,
            "qqew_freshness_guard": qqew_freshness_guard,
            "continuity_180s_guard": continuity_180s_guard,
            "data_integrity_fail_closed_guard": data_integrity_fail_closed_guard,
            "atr_ready_guard": atr_ready_guard,
            "vwap_ready_guard": vwap_ready_guard,
            "volume_ready_guard": volume_ready_guard,
        }
        self._directions: Dict[str, DirectionFn] = {
            "baseline_direction": baseline_direction,
            "fast_drift_direction": fast_drift_direction,
            "ab_direction": ab_direction,
            "elite_direction": elite_direction,
        }
        self._overlays: Dict[str, OverlayFn] = {
            "extreme_overlay": extreme_overlay,
            "anti_chase_overlay": anti_chase_overlay,
            "reentry_cooldown_overlay": reentry_cooldown_overlay,
            "high_vol_confirm_veto": high_vol_confirm_veto,
            "day_shape_overlay": day_shape_overlay,
            "v47_chase_guard_overlay": v47_chase_guard_overlay,
        }
        self._translators: Dict[str, TranslatorFn] = {
            "qqq_option_translation": qqq_option_translation,
            "tqqq_sqqq_translation": tqqq_sqqq_translation,
        }

    def evaluate(self, *, runtime, extras: Dict[str, object], recipe: EntryRecipe) -> EntryEvaluation:
        ctx = build_entry_context(runtime, extras)
        session_decision = session_window_guard(ctx)
        if not session_decision.allowed:
            denied = TranslationDecision(allowed=False, reason=session_decision.reason, side=None, stock_side="NONE", payload=dict(session_decision.payload or {}))
            return EntryEvaluation(translation=denied, context=ctx, gate_reason=session_decision.reason, gate_payload=dict(session_decision.payload or {}))

        for guard_id in recipe.guards:
            decision = self._guards[guard_id](ctx)
            if not decision.allowed:
                denied = TranslationDecision(allowed=False, reason=decision.reason, side=None, stock_side="NONE", payload=dict(decision.payload or {}))
                return EntryEvaluation(translation=denied, context=ctx, gate_reason=decision.reason, gate_payload=dict(decision.payload or {}))

        direction = self._directions[recipe.direction_module](ctx)
        if direction.direction == "NONE":
            denied = TranslationDecision(allowed=False, reason=direction.reason, side=None, stock_side="NONE", payload=dict(direction.payload or {}))
            return EntryEvaluation(translation=denied, context=ctx, gate_reason=direction.reason, gate_payload=dict(direction.payload or {}))

        for overlay_id in recipe.overlays:
            direction = self._overlays[overlay_id](ctx, direction)
            if direction.direction == "NONE":
                denied = TranslationDecision(allowed=False, reason=direction.reason, side=None, stock_side="NONE", payload=dict(direction.payload or {}))
                return EntryEvaluation(translation=denied, context=ctx, gate_reason=direction.reason, gate_payload=dict(direction.payload or {}))

        translation = self._translators[recipe.translator](ctx, direction)
        return EntryEvaluation(
            translation=translation,
            context=ctx,
            gate_reason=translation.reason,
            gate_payload=dict(translation.payload or {}),
        )
