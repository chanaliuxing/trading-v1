import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.trace import build_idempotency_key, build_trace_context


def test_build_idempotency_key_is_deterministic():
    k1 = build_idempotency_key(
        strategy_id="s1",
        strategy_run_id="run1",
        decision_id="d1",
        symbol="TQQQ",
        target_qty=10.0,
        ts_us=1_700_000_000_123_456,
    )
    k2 = build_idempotency_key(
        strategy_id="s1",
        strategy_run_id="run1",
        decision_id="d1",
        symbol="TQQQ",
        target_qty=10.0,
        ts_us=1_700_000_000_999_999,
    )
    assert k1 == k2
    assert k1.startswith("idem:")


def test_trace_context_marks_synthetic_fields_when_missing():
    ctx = build_trace_context(
        decision_id="d1",
        strategy_id="s1",
        metadata={},
        envelope_extras={},
    )
    assert ctx.compat_mode is True
    assert "strategy_run_id" in ctx.synthetic_fields
    assert "trigger_id" in ctx.synthetic_fields
    assert "trace_id" in ctx.synthetic_fields
