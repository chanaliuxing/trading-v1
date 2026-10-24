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


def _ri(value, source: str = "MANUAL") -> ResolvedInput:
    return ResolvedInput(value=value, source=source, ts=now_iso(), is_manual_lock=False)


def _ctx(resolved) -> GatekeeperContext:
    cache = GatekeeperCache()
    state = GatekeeperState()
    return GatekeeperContext(
        resolved_inputs=resolved,
        config={},
        cache=cache,
        state=state,
        symbol="META",
        anchor_symbol="QQQ",
        profile="gatekeeper_v1_1",
    )


def test_data_checks_missing_core_returns_wait() -> None:
    resolved = {
        "anchor_price": _ri(100.0, "IBKR"),
        "anchor_vwap": _ri(None, "MISSING"),
        "vix": _ri(18.0, "IBKR"),
        "vvix": _ri(100.0, "IBKR"),
        "ticker_price": _ri(200.0, "IBKR"),
        "ticker_vwap": _ri(199.0, "CACHE"),
        "ivr": _ri(25.0, "IBKR"),
        "skew_value": _ri(3.0, "IBKR"),
        "spread_width_usd": _ri(5.0, "MANUAL"),
    }
    step = run_data_checks(_ctx(resolved))
    assert step.stop is True
    assert step.outputs_set.get("structure") == "FLAT"
    assert "anchor_vwap" in (step.missing_required or [])


def test_data_checks_missing_ivr_returns_wait() -> None:
    resolved = {
        "anchor_price": _ri(100.0, "IBKR"),
        "anchor_vwap": _ri(99.5, "CACHE"),
        "vix": _ri(18.0, "IBKR"),
        "vvix": _ri(100.0, "IBKR"),
        "ticker_price": _ri(200.0, "IBKR"),
        "ticker_vwap": _ri(199.0, "CACHE"),
        "ivr": _ri(None, "MISSING"),
        "skew_value": _ri(3.0, "IBKR"),
        "spread_width_usd": _ri(5.0, "MANUAL"),
    }
    step = run_data_checks(_ctx(resolved))
    assert step.stop is True
    assert "ivr" in (step.missing_required or [])
