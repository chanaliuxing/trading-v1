from __future__ import annotations

import sys
from pathlib import Path


def _add_gatekeeper_to_path() -> None:
    root = Path(__file__).resolve()
    for _ in range(5):
        root = root.parent
    sys.path.insert(0, str(root / "tool" / "gatekeeper"))


_add_gatekeeper_to_path()

from core.cache import GatekeeperCache  # noqa: E402
from core.context import GatekeeperContext, GatekeeperState  # noqa: E402
from core.time_utils import now_iso  # noqa: E402
from core.types import ResolvedInput  # noqa: E402
from rules.v1_1_market_mode import run as run_market_mode  # noqa: E402


def _ri(value, source: str = "MANUAL") -> ResolvedInput:
    return ResolvedInput(value=value, source=source, ts=now_iso(), is_manual_lock=False)


def _ctx(
    *,
    vvix: float,
    anchor_price: float,
    anchor_vwap: float,
    chop_score: float,
    event_confirmed: bool = False,
    bofa_9_3: float | None = None,
    drop15m: float | None = None,
) -> GatekeeperContext:
    resolved = {
        "vvix": _ri(vvix, "IBKR"),
        "anchor_price": _ri(anchor_price, "IBKR"),
        "anchor_vwap": _ri(anchor_vwap, "CACHE"),
        "anchor_chop_score": _ri(chop_score, "CACHE"),
        "event_confirmed": _ri(event_confirmed, "MANUAL"),
        "bofa_9_3": _ri(bofa_9_3, "MANUAL") if bofa_9_3 is not None else _ri(None, "MISSING"),
        "anchor_drop_15m_pct": _ri(drop15m, "CACHE") if drop15m is not None else _ri(None, "MISSING"),
    }
    config = {"thresholds": {"vvix": {"hedge_min": 105, "crash_min": 110}, "bofa_9_3": {"weekly_red_min": 8}}, "chop": {"score_trigger": 1.0}}
    cache = GatekeeperCache()
    state = GatekeeperState()
    return GatekeeperContext(
        resolved_inputs=resolved,
        config=config,
        cache=cache,
        state=state,
        symbol="QQQ",
        anchor_symbol="QQQ",
        profile="gatekeeper_v1_1",
    )


def test_market_mode_crash_on_multiple_hits() -> None:
    ctx = _ctx(vvix=112, anchor_price=98, anchor_vwap=100, chop_score=0.5, event_confirmed=True)
    step = run_market_mode(ctx)
    assert step.outputs_set["market_mode"] == "CRASH"


def test_market_mode_chop_when_choppy() -> None:
    ctx = _ctx(vvix=90, anchor_price=100, anchor_vwap=100, chop_score=1.2)
    step = run_market_mode(ctx)
    assert step.outputs_set["market_mode"] == "CHOP"
