import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.order_event_writer import emit_order_event


class _PG:
    def __init__(self, *, fail: bool = False):
        if fail:
            self.execute = AsyncMock(side_effect=RuntimeError('relation "core.core_order_event" does not exist'))
        else:
            self.execute = AsyncMock(return_value="INSERT 0 1")


def _kwargs():
    return dict(
        event_type="intent_received",
        trace_id="t1",
        strategy_id="s1",
        strategy_run_id="r1",
        decision_id="d1",
        symbol="TQQQ",
    )


def test_emit_order_event_success():
    pg = _PG(fail=False)
    ok = __import__("asyncio").run(emit_order_event(pg=pg, enabled=True, **_kwargs()))
    assert ok is True
    assert pg.execute.await_count == 1


def test_emit_order_event_missing_table_is_non_blocking():
    pg = _PG(fail=True)
    ok = __import__("asyncio").run(emit_order_event(pg=pg, enabled=True, **_kwargs()))
    assert ok is False
    assert pg.execute.await_count == 1


def test_emit_order_event_accepts_datetime_payload():
    pg = _PG(fail=False)
    ok = __import__("asyncio").run(
        emit_order_event(
            pg=pg,
            enabled=True,
            **_kwargs(),
            payload={"ts": datetime(2026, 4, 1, 13, 55, 5), "nested": {"when": datetime(2026, 4, 1, 13, 55, 6)}},
        )
    )
    assert ok is True
    assert pg.execute.await_count == 1
