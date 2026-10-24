from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Sequence

from libs.strategy.base import DecisionEnvelope

from .types import ParityDiff


def normalize_envelope(envelope: DecisionEnvelope) -> Dict[str, Any]:
    extras = dict(envelope.extras or {})
    ignore = {
        "strategy_run_id",
        "trace_id",
        "trigger_id",
        "decision_snapshot_id",
        "decision_snapshot_key",
        "position_id",
        "allocation_id",
        "parity_profile_id",
        "execution_profile_id",
        "order_ref",
        "definition_id",
        "audit_namespace",
        "position_namespace",
    }
    cleaned = {k: v for k, v in extras.items() if k not in ignore}
    return {
        "symbol": str(envelope.symbol),
        "target_qty": float(envelope.target_qty),
        "order_style": {
            "kind": str(envelope.order_style.kind),
            "limit_px": envelope.order_style.limit_px,
            "stop_px": envelope.order_style.stop_px,
            "tif": str(getattr(envelope.order_style, "tif", "") or ""),
        },
        "extras": cleaned,
    }


class OptionParityRunner:
    def compare_envelopes(
        self,
        old_envelopes: Sequence[DecisionEnvelope],
        new_envelopes: Sequence[DecisionEnvelope],
    ) -> Dict[str, Any]:
        old_norm = [normalize_envelope(env) for env in old_envelopes]
        new_norm = [normalize_envelope(env) for env in new_envelopes]
        diffs: List[Dict[str, Any]] = []
        max_len = max(len(old_norm), len(new_norm))
        for idx in range(max_len):
            old_item = old_norm[idx] if idx < len(old_norm) else None
            new_item = new_norm[idx] if idx < len(new_norm) else None
            if old_item != new_item:
                diffs.append(
                    {
                        "index": idx,
                        "old": old_item,
                        "new": new_item,
                    }
                )
        return {
            "old_count": len(old_norm),
            "new_count": len(new_norm),
            "matched": len(diffs) == 0 and len(old_norm) == len(new_norm),
            "diffs": diffs,
        }

    def compare_fields(self, *, old_value: Dict[str, Any], new_value: Dict[str, Any]) -> List[ParityDiff]:
        diffs: List[ParityDiff] = []
        keys = sorted(set(old_value.keys()) | set(new_value.keys()))
        for key in keys:
            if old_value.get(key) != new_value.get(key):
                diffs.append(
                    ParityDiff(
                        field=str(key),
                        old_value=old_value.get(key),
                        new_value=new_value.get(key),
                    )
                )
        return diffs
