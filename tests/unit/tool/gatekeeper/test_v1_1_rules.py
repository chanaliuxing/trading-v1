from __future__ import annotations

import sys
from pathlib import Path


def _add_gatekeeper_to_path() -> None:
    root = Path(__file__).resolve()
    # tests/unit/tool/gatekeeper/test_*.py -> repo root
    for _ in range(5):
        root = root.parent
    gatekeeper_dir = root / "tool" / "gatekeeper"
    sys.path.insert(0, str(gatekeeper_dir))


_add_gatekeeper_to_path()

from core.cache import GatekeeperCache  # noqa: E402
from core.context import GatekeeperContext, GatekeeperState  # noqa: E402
from core.time_utils import now_iso  # noqa: E402
from core.types import ResolvedInput  # noqa: E402
from rules.v1_1_constraints import run as run_constraints  # noqa: E402
from rules.v1_1_css_gate import run as run_css_gate  # noqa: E402
from rules.v1_1_structure_select import run as run_structure_select  # noqa: E402


def _ri(value, source: str = "MANUAL") -> ResolvedInput:
    return ResolvedInput(value=value, source=source, ts=now_iso(), is_manual_lock=False)


def _ctx(
    *,
    symbol: str = "QQQ",
    event_level: str = "None",
    event_confirmed: bool = False,
    bofa_9_3: float | None = None,
    skew_value: float | None = 0.0,
    ivr: float | None = 30.0,
    css: int | None = 55,
    spread_width_usd: float | None = 5.0,
    market_mode: str = "CARRY",
    bias: str = "BULLISH",
) -> GatekeeperContext:
    resolved = {
        "event_level": _ri(event_level),
        "event_confirmed": _ri(event_confirmed),
        "bofa_9_3": _ri(bofa_9_3, "MANUAL") if bofa_9_3 is not None else _ri(None, "MISSING"),
        "skew_value": _ri(skew_value, "IBKR") if skew_value is not None else _ri(None, "MISSING"),
        "ivr": _ri(ivr, "IBKR") if ivr is not None else _ri(None, "MISSING"),
        "css": _ri(css, "CACHE") if css is not None else _ri(None, "MISSING"),
        "spread_width_usd": _ri(spread_width_usd, "MANUAL") if spread_width_usd is not None else _ri(None, "MISSING"),
    }
    config = {
        "templates": {
            "bps_bcs": {
                "dte": 14,
                "short_delta_range": [0.1, 0.2],
                "defensive_short_delta_range": [0.08, 0.15],
            },
            "ic": {"dte_range": [14, 21], "short_delta_range": [0.1, 0.15]},
        }
    }
    cache = GatekeeperCache()
    state = GatekeeperState()
    state.market_mode = market_mode
    state.bias = bias
    return GatekeeperContext(
        resolved_inputs=resolved,
        config=config,
        cache=cache,
        state=state,
        symbol=symbol,
        anchor_symbol="QQQ",
        profile="gatekeeper_v1_1",
    )


def test_constraints_event_high_forbids_credit() -> None:
    ctx = _ctx(symbol="META", event_level="High", skew_value=6.0, bofa_9_3=9.0)
    step = run_constraints(ctx)
    forbidden = set(step.outputs_set.get("constraints.forbidden_structures") or [])
    assert step.outputs_set["flags.forbid_credit"] is True
    assert {"BPS", "BCS", "IC"}.issubset(forbidden)


def test_constraints_index_rr25_extreme_forbids_bps_ic() -> None:
    ctx = _ctx(symbol="QQQ", event_level="None", skew_value=7.5)
    step = run_constraints(ctx)
    forbidden = set(step.outputs_set.get("constraints.forbidden_structures") or [])
    assert "BPS" in forbidden
    assert "IC" in forbidden


def test_css_gate_no_carry() -> None:
    ctx = _ctx(css=25, market_mode="CARRY", event_level="None")
    step = run_css_gate(ctx)
    assert step.outputs_set["flags.carry_gate"] == "NO_CARRY"


def test_structure_select_no_carry_switches_to_debit() -> None:
    ctx = _ctx(market_mode="CARRY", bias="BULLISH", css=25)
    ctx.state.flags["carry_gate"] = "NO_CARRY"
    step = run_structure_select(ctx)
    assert step.outputs_set["structure"] == "DEBIT_CALL_SPREAD"


def test_structure_select_neutral_event_medium_wait_with_ic_alt() -> None:
    ctx = _ctx(market_mode="CARRY", bias="NEUTRAL", event_level="Medium", ivr=30.0, css=60)
    ctx.state.flags["carry_gate"] = "NORMAL"
    step = run_structure_select(ctx)
    assert step.outputs_set["structure"] == "FLAT"
    alts = step.outputs_set.get("flags.alternatives") or []
    assert any(a.get("structure") == "IC" for a in alts)


def test_structure_select_neutral_ivr_low_wait() -> None:
    ctx = _ctx(market_mode="CARRY", bias="NEUTRAL", event_level="None", ivr=15.0, css=60)
    ctx.state.flags["carry_gate"] = "NORMAL"
    step = run_structure_select(ctx)
    assert step.outputs_set["structure"] == "FLAT"


def test_structure_select_rr25_missing_wait_in_carry() -> None:
    ctx = _ctx(market_mode="CARRY", bias="BULLISH", skew_value=None, css=60)
    ctx.state.flags["carry_gate"] = "NORMAL"
    step = run_structure_select(ctx)
    assert step.outputs_set["structure"] == "FLAT"
    assert "skew_value" in (step.missing_required or [])

