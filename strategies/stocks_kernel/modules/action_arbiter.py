from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionArbiterDecision:
    winning_actions: tuple[Any, ...]
    blocked_actions: tuple[Any, ...]
    freeze_applied: bool
    reason: str
    highest_rank: int


class ExecutionPriorityResolver:
    """
    Resolve action priority using a stable, platform-wide order.

    Lower rank means higher priority.
    """

    _RANKS = {
        "FLATTEN": 0,
        "CANCEL_WORKING": 0,
        "STOP": 1,
        "ENSURE_PROTECTION": 1,
        "TAKE_PROFIT": 2,
        "RETREAT": 2,
        "REDUCE": 2,
        "ENTRY": 3,
        "ADD": 4,
    }

    def rank(self, action_type: str) -> int:
        return int(self._RANKS.get(str(action_type or "").strip().upper(), 99))

    def ordered(self, actions: tuple[Any, ...]) -> tuple[Any, ...]:
        indexed = list(enumerate(actions))
        indexed.sort(
            key=lambda item: (
                self.rank(getattr(item[1], "action_type", "")),
                int(getattr(item[1], "priority", 100)),
                item[0],
            )
        )
        return tuple(item[1] for item in indexed)


class ActionArbiter:
    """
    Build a coordinated winning action set for one evaluation tick.
    """

    def __init__(self, *, resolver: ExecutionPriorityResolver | None = None) -> None:
        self._resolver = resolver or ExecutionPriorityResolver()

    def arbitrate(
        self,
        *,
        action_plans: tuple[Any, ...],
        freeze_entry: bool = False,
        max_entry_actions_per_tick: int | None = None,
        max_entry_total_qty_per_tick: float | None = None,
    ) -> ActionArbiterDecision:
        ordered = self._resolver.ordered(tuple(action_plans or ()))
        if not ordered:
            return ActionArbiterDecision(
                winning_actions=(),
                blocked_actions=(),
                freeze_applied=bool(freeze_entry),
                reason="no_action",
                highest_rank=99,
            )
        blocked: list[Any] = []
        eligible: list[Any] = []
        for action in ordered:
            action_type = str(getattr(action, "action_type", "") or "").strip().upper()
            if freeze_entry and action_type in {"ENTRY", "ADD"}:
                blocked.append(action)
                continue
            eligible.append(action)
        if not eligible:
            return ActionArbiterDecision(
                winning_actions=(),
                blocked_actions=tuple(blocked),
                freeze_applied=bool(freeze_entry),
                reason="entry_frozen",
                highest_rank=99,
            )
        winning: list[Any] = []
        highest_rank = 99
        accepted_entry_actions = 0
        accepted_entry_total_qty = 0.0
        for action in eligible:
            action_type = str(getattr(action, "action_type", "") or "").strip().upper()
            if self._is_entry_family(action_type):
                if max_entry_actions_per_tick is not None and max_entry_actions_per_tick > 0:
                    if accepted_entry_actions >= int(max_entry_actions_per_tick):
                        blocked.append(action)
                        continue
                if max_entry_total_qty_per_tick is not None and max_entry_total_qty_per_tick > 0.0:
                    try:
                        action_qty = max(0.0, float(getattr(action, "qty", 0.0) or 0.0))
                    except Exception:
                        action_qty = 0.0
                    if action_qty > 0.0 and (accepted_entry_total_qty + action_qty) > float(max_entry_total_qty_per_tick):
                        blocked.append(action)
                        continue
            action_rank = self._resolver.rank(action_type)
            if not winning:
                winning.append(action)
                highest_rank = action_rank
                if self._is_entry_family(action_type):
                    accepted_entry_actions += 1
                    try:
                        accepted_entry_total_qty += max(0.0, float(getattr(action, "qty", 0.0) or 0.0))
                    except Exception:
                        pass
                continue
            if not self._is_compatible_with_winning(action=action, winning=tuple(winning)):
                blocked.append(action)
                continue
            winning.append(action)
            if self._is_entry_family(action_type):
                accepted_entry_actions += 1
                try:
                    accepted_entry_total_qty += max(0.0, float(getattr(action, "qty", 0.0) or 0.0))
                except Exception:
                    pass
        reason = "ready"
        if freeze_entry and blocked:
            reason = "entry_frozen"
        if blocked and reason == "ready":
            reason = "suppressed_lower_priority"
            if max_entry_actions_per_tick is not None or max_entry_total_qty_per_tick is not None:
                reason = "entry_budget_suppressed"
        return ActionArbiterDecision(
            winning_actions=tuple(winning),
            blocked_actions=tuple(blocked),
            freeze_applied=bool(freeze_entry),
            reason=reason,
            highest_rank=highest_rank,
        )

    @staticmethod
    def _is_helper_action(action_type: str) -> bool:
        return action_type in {"CANCEL_WORKING", "ENSURE_PROTECTION"}

    @staticmethod
    def _scope_key(action: Any) -> tuple[str, ...]:
        lot_id = str(getattr(action, "position_lot_id", "") or "").strip()
        if lot_id:
            return ("lot", lot_id)
        symbol = str(getattr(action, "symbol", "") or "").strip().upper()
        lane_id = str(getattr(action, "lane_id", "") or "").strip()
        if lane_id:
            return ("lane", symbol, lane_id)
        if symbol:
            return ("symbol", symbol)
        return ("global",)

    @staticmethod
    def _is_entry_family(action_type: str) -> bool:
        return action_type in {"ENTRY", "ADD"}

    @staticmethod
    def _is_rank_zero(action_type: str) -> bool:
        return action_type in {"FLATTEN"}

    def _is_compatible_with_winning(self, *, action: Any, winning: tuple[Any, ...]) -> bool:
        action_type = str(getattr(action, "action_type", "") or "").strip().upper()
        action_scope = self._scope_key(action)
        winning_items = tuple(winning or ())
        winner_types = tuple(
            str(getattr(item, "action_type", "") or "").strip().upper()
            for item in winning_items
        )
        helper_types = {item for item in winner_types if self._is_helper_action(item)}
        primary_items = tuple(
            item
            for item in winning_items
            if not self._is_helper_action(str(getattr(item, "action_type", "") or "").strip().upper())
        )

        if self._is_helper_action(action_type):
            if any(
                str(getattr(item, "action_type", "") or "").strip().upper() == action_type
                and self._scope_key(item) == action_scope
                for item in winning_items
            ):
                return False
            if not primary_items:
                return True
            if action_type == "ENSURE_PROTECTION":
                # Re-arm protection only when we already have a scoped reduce/take-profit winner.
                for primary in primary_items:
                    primary_type = str(getattr(primary, "action_type", "") or "").strip().upper()
                    if self._scope_key(primary) != action_scope:
                        continue
                    if self._helper_allowed_for_primary(helper_type=action_type, primary_type=primary_type):
                        return True
                return False
            if action_type == "CANCEL_WORKING":
                # Cancel can accompany emergency exits, but must not pair with scoped entry/add.
                for primary in primary_items:
                    primary_type = str(getattr(primary, "action_type", "") or "").strip().upper()
                    if self._scope_key(primary) != action_scope:
                        continue
                    if self._is_entry_family(primary_type):
                        return False
                return True
            return False

        action_rank = self._resolver.rank(action_type)
        for primary in primary_items:
            primary_type = str(getattr(primary, "action_type", "") or "").strip().upper()
            if self._scope_key(primary) != action_scope:
                continue
            primary_rank = self._resolver.rank(primary_type)
            if self._is_rank_zero(primary_type) or self._is_rank_zero(action_type):
                if primary_type != action_type:
                    return False
                continue
            if self._is_entry_family(primary_type) and self._is_entry_family(action_type):
                continue
            if self._is_entry_family(primary_type) != self._is_entry_family(action_type):
                return False
            if primary_type != action_type:
                return False
            if action_rank != primary_rank:
                return False

        # Do not allow helper/primary contracts that are semantically invalid on same scope.
        for helper_type in helper_types:
            if helper_type == "ENSURE_PROTECTION" and action_scope in {
                self._scope_key(item) for item in winning_items if str(getattr(item, "action_type", "") or "").strip().upper() == "ENSURE_PROTECTION"
            }:
                if not self._helper_allowed_for_primary(
                    helper_type=helper_type,
                    primary_type=action_type,
                ):
                    return False
        return True

    @staticmethod
    def _helper_allowed_for_primary(*, helper_type: str, primary_type: str) -> bool:
        primary = str(primary_type or "").strip().upper()
        helper = str(helper_type or "").strip().upper()
        if helper == "CANCEL_WORKING":
            return primary not in {"ENTRY", "ADD"}
        if helper == "ENSURE_PROTECTION":
            return primary in {"TAKE_PROFIT", "REDUCE"}
        return False
