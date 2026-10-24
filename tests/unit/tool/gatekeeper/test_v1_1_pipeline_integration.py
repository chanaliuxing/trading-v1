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
from rules.data_checks import run as run_data_checks  # noqa: E402
from rules.v1_1_constraints import run as run_constraints  # noqa: E402
from rules.v1_1_css_gate import run as run_css_gate  # noqa: E402
from rules.v1_1_market_mode import run as run_market_mode  # noqa: E402
from rules.v1_1_structure_select import run as run_structure_select  # noqa: E402
from rules.vwap_gate import run as run_vwap_gate  # noqa: E402


def _ri(value, source: str = "MANUAL") -> ResolvedInput:
    return ResolvedInput(value=value, source=source, ts=now_iso(), is_manual_lock=False)


def _apply_outputs(state: GatekeeperState, outputs: dict[str, object]) -> None:
    for key, value in outputs.items():
        if key == "market_mode":
            state.market_mode = str(value)
        elif key == "bias":
            state.bias = str(value)
        elif key == "structure":
            state.structure = str(value)
        elif key == "params" and isinstance(value, dict):
            state.params = value
        elif key.startswith("flags."):
            state.flags[key.split(".", 1)[1]] = value
        elif key == "constraints.forbidden_structures":
            state.constraints.forbidden_structures = set(value or [])
        elif key == "constraints.notes":
            state.constraints.notes = list(value or [])


def test_pipeline_bullish_carry_recommends_bps() -> None:
    bars = [
        {"close": 100.0, "high": 100.2, "low": 99.8, "volume": 1000},
        {"close": 100.1, "high": 100.3, "low": 99.9, "volume": 900},
    ]
    resolved = {
        "anchor_price": _ri(100.0, "IBKR"),
        "anchor_vwap": _ri(99.5, "CACHE"),
        "vix": _ri(18.0, "IBKR"),
        "vvix": _ri(95.0, "IBKR"),
        "anchor_chop_score": _ri(0.4, "CACHE"),
        "event_confirmed": _ri(False, "MANUAL"),
        "event_level": _ri("None", "MANUAL"),
        "bofa_9_3": _ri(5.0, "MANUAL"),
        "anchor_drop_15m_pct": _ri(0.2, "CACHE"),
        "ticker_bars_1m": _ri(bars, "IBKR"),
        "ticker_vwap": _ri(99.5, "CACHE"),
        "ticker_price": _ri(100.2, "IBKR"),
        "ivr": _ri(40.0, "IBKR"),
        "skew_value": _ri(2.0, "IBKR"),
        "atm_spread_pct": _ri(0.6, "IBKR"),
        "css": _ri(65, "CACHE"),
        "spread_width_usd": _ri(5.0, "MANUAL"),
    }
    config = {
        "thresholds": {"vvix": {"hedge_min": 105, "crash_min": 110}, "bofa_9_3": {"weekly_red_min": 8}},
        "chop": {"score_trigger": 1.0},
        "templates": {
            "bps_bcs": {"dte": 14, "short_delta_range": [0.1, 0.2], "defensive_short_delta_range": [0.08, 0.15]},
            "ic": {"dte_range": [14, 21], "short_delta_range": [0.1, 0.15]},
        },
        "vwap_gate": {"neutral_band_pct": 0.0015},
    }
    cache = GatekeeperCache()
    state = GatekeeperState()
    ctx = GatekeeperContext(
        resolved_inputs=resolved,
        config=config,
        cache=cache,
        state=state,
        symbol="META",
        anchor_symbol="QQQ",
        profile="gatekeeper_v1_1",
    )

    step = run_data_checks(ctx)
    assert step.stop is False

    step = run_market_mode(ctx)
    _apply_outputs(state, step.outputs_set)
    assert state.market_mode == "CARRY"

    step = run_vwap_gate(ctx)
    _apply_outputs(state, step.outputs_set)
    assert state.bias == "BULLISH"

    step = run_constraints(ctx)
    _apply_outputs(state, step.outputs_set)

    step = run_css_gate(ctx)
    _apply_outputs(state, step.outputs_set)

    step = run_structure_select(ctx)
    _apply_outputs(state, step.outputs_set)
    assert state.structure == "BPS"
