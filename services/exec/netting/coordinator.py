from __future__ import annotations

import hashlib
from typing import Iterable, List, Tuple

from .models import NetChildIntent, NetParentPlan, NettingDecision


class NettingCoordinator:
    """
    Pure in-memory netting helper.

    This module intentionally does not place orders and is not wired into runtime yet.
    """

    def __init__(self, *, max_batch_size: int = 64):
        self.max_batch_size = max(1, int(max_batch_size))

    def build_group(self, intents: Iterable[NetChildIntent]) -> NettingDecision:
        normalized = [i.normalized() for i in intents]
        if not normalized:
            return NettingDecision(status="EMPTY", reason="no_intents")

        limited = normalized[: self.max_batch_size]
        accepted, rejected, reason = self._filter_compatible(limited)
        if not accepted:
            return NettingDecision(
                status="REJECTED",
                reason=reason,
                accepted_children=[],
                rejected_children=rejected,
            )

        sorted_children = sorted(
            accepted,
            key=lambda x: (-int(x.priority), int(x.submitted_at_us), str(x.strategy_id), str(x.decision_id)),
        )
        qty = float(sum(max(0.0, float(x.qty)) for x in sorted_children))
        if qty <= 0:
            return NettingDecision(
                status="REJECTED",
                reason="non_positive_qty",
                accepted_children=[],
                rejected_children=accepted + rejected,
            )

        symbol = sorted_children[0].symbol
        side = sorted_children[0].side
        key_parts = [f"{symbol}:{side}"] + [f"{x.strategy_id}:{x.decision_id}:{x.qty}" for x in sorted_children]
        group_key = "|".join(key_parts)
        group_id = hashlib.sha1(group_key.encode("utf-8")).hexdigest()[:16]
        child_keys = [f"{x.strategy_id}:{x.decision_id}" for x in sorted_children]

        return NettingDecision(
            status="NETTED",
            reason="ok",
            parent_plan=NetParentPlan(group_id=group_id, symbol=symbol, side=side, qty=qty, child_keys=child_keys),
            accepted_children=sorted_children,
            rejected_children=rejected,
        )

    def _filter_compatible(self, intents: List[NetChildIntent]) -> Tuple[List[NetChildIntent], List[NetChildIntent], str]:
        base = intents[0]
        accepted: List[NetChildIntent] = []
        rejected: List[NetChildIntent] = []
        for intent in intents:
            if intent.symbol != base.symbol:
                rejected.append(intent)
                continue
            if intent.side != base.side:
                rejected.append(intent)
                continue
            if float(intent.qty) <= 0:
                rejected.append(intent)
                continue
            accepted.append(intent)

        if not accepted:
            return [], rejected, "no_compatible_intents"

        if rejected:
            return accepted, rejected, "partial_reject"
        return accepted, rejected, "ok"
