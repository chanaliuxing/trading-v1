from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _uuid() -> str:
    # Prefer UUIDv7 when available (Py3.13+), fallback to UUIDv4.
    try:
        if hasattr(uuid, "uuid7"):
            return str(uuid.uuid7())  # type: ignore[attr-defined]
    except Exception:
        pass
    return str(uuid.uuid4())


def _clean(v: Any) -> str:
    return str(v or "").strip()


def normalize_qty(v: Any) -> str:
    try:
        return f"{float(v):.8f}"
    except Exception:
        return "0.00000000"


def ts_bucket_us(ts_us: Optional[Any], bucket_sec: int = 1) -> int:
    try:
        ts = int(ts_us)
    except Exception:
        ts = int(time.time() * 1_000_000)
    sec_us = max(1, int(bucket_sec)) * 1_000_000
    return (ts // sec_us) * sec_us


@dataclass
class TraceContext:
    strategy_id: str
    strategy_run_id: str
    trigger_id: str
    decision_id: str
    trace_id: str
    intent_id: Optional[str] = None
    causation_id: Optional[str] = None
    compat_mode: bool = False
    synthetic_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_run_id": self.strategy_run_id,
            "trigger_id": self.trigger_id,
            "decision_id": self.decision_id,
            "trace_id": self.trace_id,
            "intent_id": self.intent_id,
            "causation_id": self.causation_id,
            "compat_mode": self.compat_mode,
            "synthetic_fields": list(self.synthetic_fields),
        }


def build_trace_context(
    *,
    decision_id: str,
    strategy_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    envelope_extras: Optional[Dict[str, Any]] = None,
) -> TraceContext:
    meta = dict(metadata or {})
    extras = dict(envelope_extras or {})

    synthetic: List[str] = []
    strategy_run_id = _clean(meta.get("strategy_run_id") or extras.get("strategy_run_id"))
    if not strategy_run_id:
        strategy_run_id = _uuid()
        synthetic.append("strategy_run_id")

    trigger_id = _clean(meta.get("trigger_id") or extras.get("trigger_id"))
    if not trigger_id:
        trigger_id = f"synthetic:{_uuid()}"
        synthetic.append("trigger_id")

    trace_id = _clean(meta.get("trace_id") or extras.get("trace_id"))
    if not trace_id:
        trace_id = _uuid()
        synthetic.append("trace_id")

    causation_id = _clean(meta.get("causation_id") or extras.get("causation_id")) or None
    intent_id = _clean(meta.get("intent_id") or extras.get("intent_id")) or None

    return TraceContext(
        strategy_id=_clean(strategy_id),
        strategy_run_id=strategy_run_id,
        trigger_id=trigger_id,
        decision_id=_clean(decision_id),
        trace_id=trace_id,
        intent_id=intent_id,
        causation_id=causation_id,
        compat_mode=bool(synthetic),
        synthetic_fields=synthetic,
    )


def validate_trace_context(ctx: TraceContext) -> List[str]:
    missing: List[str] = []
    for field_name in ("strategy_id", "decision_id", "trace_id"):
        if not _clean(getattr(ctx, field_name)):
            missing.append(field_name)
    # strategy_run_id/trigger_id can be synthesized, but still required post-enrichment.
    for field_name in ("strategy_run_id", "trigger_id"):
        if not _clean(getattr(ctx, field_name)):
            missing.append(field_name)
    return missing


def build_idempotency_key(
    *,
    strategy_id: str,
    strategy_run_id: str,
    decision_id: str,
    symbol: str,
    target_qty: Any,
    ts_us: Optional[Any],
) -> str:
    bucket = ts_bucket_us(ts_us, bucket_sec=1)
    raw = "|".join(
        [
            _clean(strategy_id),
            _clean(strategy_run_id),
            _clean(decision_id),
            _clean(symbol).upper(),
            normalize_qty(target_qty),
            str(bucket),
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"idem:{digest}"
