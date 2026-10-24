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
from rules.vwap_gate import run as run_vwap_gate  # noqa: E402


def _ri(value, source: str = "MANUAL") -> ResolvedInput:
    return ResolvedInput(value=value, source=source, ts=now_iso(), is_manual_lock=False)


def _ctx(bars, vwap: float, *, last_price: float | None, neutral_band_pct: float = 0.0015) -> GatekeeperContext:
    resolved = {
        "ticker_bars_1m": _ri(bars, "IBKR"),
        "ticker_vwap": _ri(vwap, "CACHE"),
        "ticker_price": _ri(last_price, "IBKR") if last_price is not None else _ri(None, "MISSING"),
    }
    config = {"vwap_gate": {"neutral_band_pct": neutral_band_pct}}
    cache = GatekeeperCache()
    state = GatekeeperState()
    return GatekeeperContext(
        resolved_inputs=resolved,
        config=config,
        cache=cache,
        state=state,
        symbol="META",
        anchor_symbol="QQQ",
        profile="gatekeeper_v1_1",
    )


def test_vwap_gate_bullish_with_price_above_band() -> None:
    bars = [
        {"close": 100.0, "high": 100.2, "low": 99.8, "volume": 1000},
        {"close": 100.1, "high": 100.3, "low": 99.9, "volume": 800},
    ]
    ctx = _ctx(bars, vwap=99.5, last_price=100.2)
    step = run_vwap_gate(ctx)
    assert step.outputs_set["bias"] == "BULLISH"


def test_vwap_gate_neutral_inside_band() -> None:
    bars = [
        {"close": 100.0, "high": 100.1, "low": 99.9, "volume": 1000},
        {"close": 100.0, "high": 100.1, "low": 99.9, "volume": 800},
    ]
    ctx = _ctx(bars, vwap=100.0, last_price=100.05, neutral_band_pct=0.0015)
    step = run_vwap_gate(ctx)
    assert step.outputs_set["bias"] == "NEUTRAL"
