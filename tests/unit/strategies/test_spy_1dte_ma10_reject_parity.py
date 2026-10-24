from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from libs.strategy import StrategyParams
from strategies.options_kernel.parity import OptionParityRunner
from strategies.qqq_1dte_ma10_reject_live_strategy import QQQ1DTEMA10RejectLiveStrategy
from strategies.spy_1dte_ma10_reject_v2_strategy import SPY1DTEMA10RejectV2Strategy


ET = ZoneInfo("America/New_York")


def _assert_core_env_equivalence(old_env, new_env) -> None:
    assert old_env.symbol == new_env.symbol
    assert old_env.target_qty == new_env.target_qty
    assert old_env.order_style.kind == new_env.order_style.kind
    for key in (
        "instrument_type",
        "underlying",
        "bars_symbol",
        "risk_symbol",
        "contracts_mult",
        "ref_px",
        "ref_px_source",
        "opt_bid_px",
        "opt_ask_px",
        "opt_mid_px",
        "opt_last_px",
        "opt_quote_age_sec",
    ):
        assert old_env.extras.get(key) == new_env.extras.get(key)


class _DummyLogger:
    def __init__(self):
        self.warning = MagicMock()
        self.info = MagicMock()
        self.error = MagicMock()
        self.debug = MagicMock()


def _make_ctx(strategy_id: str):
    ctx = MagicMock()
    ctx.log = _DummyLogger()
    ctx.engine = MagicMock()
    ctx.subscribe_features = AsyncMock()
    ctx.enable_realtime_feature_engine = AsyncMock()
    ctx.disable_realtime_feature_engine = AsyncMock()
    ctx.portfolio = AsyncMock(return_value={"positions": []})
    ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0, avg_px=0.0))
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    ctx.strategy_id = strategy_id
    return ctx


@pytest.mark.asyncio
async def test_spy_1dte_v2_matches_legacy_entry_intent_shape(tmp_path: Path, monkeypatch):
    params_old = StrategyParams(
        extras={
            "preload_history_bars": 0,
            "underlying": "SPY",
            "risk_symbol": "OPT:SPY:1DTE",
        }
    )
    ctx_old = _make_ctx("spy_1dte_ma10_reject_legacy_test")
    old = QQQ1DTEMA10RejectLiveStrategy(ctx_old, params_old)
    await old.initialize()

    params_v2 = StrategyParams(
        extras={
            "preload_history_bars": 0,
            "live_parity_snapshot_root": str(tmp_path / "snapshots"),
            "kernel_mode": "hybrid",
        }
    )
    ctx_v2 = _make_ctx("spy_1dte_ma10_reject_v2_test")
    new = SPY1DTEMA10RejectV2Strategy(ctx_v2, params_v2)
    await new.initialize()

    now = datetime(2026, 3, 25, 11, 11, 0, tzinfo=ET)
    quote_payload = (
        {
            "opt_symbol": "OPT:SPY:20260326:P:588",
            "strike": 588.0,
            "bid": 3.90,
            "ask": 4.02,
            "mid": 3.96,
            "last": 3.98,
            "quote_age_sec": 12.0,
        },
        None,
    )
    monkeypatch.setattr(old, "_select_0dte_quote", lambda *args, **kwargs: quote_payload)
    monkeypatch.setattr(new.kernel.delegate, "_select_0dte_quote", lambda *args, **kwargs: quote_payload)
    old._pending_entry_plan = {
        "signal_kind": "trend_continuation_put",
        "structure_stop": 589.8,
        "target_1r": 587.9,
        "target_1p2r": 587.5,
        "r_per_share": 0.8,
        "initial_contracts": 4,
    }
    new.kernel.delegate._pending_entry_plan = {
        "signal_kind": "trend_continuation_put",
        "structure_stop": 589.8,
        "target_1r": 587.9,
        "target_1p2r": 587.5,
        "r_per_share": 0.8,
        "initial_contracts": 4,
    }

    audit_ids = ("t-fixed", "d-fixed", "x-fixed")
    await old._submit_entry(
        now,
        side="PUT",
        spot=588.62,
        entry_bar={"vwap": 589.10, "atr_1m": 0.62},
        spread_max_pct=0.12,
        wall_now=now,
        audit_ids=audit_ids,
    )
    await new.kernel.delegate._submit_entry(
        now,
        side="PUT",
        spot=588.62,
        entry_bar={"vwap": 589.10, "atr_1m": 0.62},
        spread_max_pct=0.12,
        wall_now=now,
        audit_ids=audit_ids,
    )

    old_env = ctx_old.submit_intent.await_args.args[0]
    new_env = ctx_v2.submit_intent.await_args.args[0]
    result = OptionParityRunner().compare_envelopes([old_env], [new_env])
    _assert_core_env_equivalence(old_env, new_env)
    assert result["old_count"] == 1
    assert result["new_count"] == 1
    assert new.kernel.state_snapshot()["ledger"]["lots"]
