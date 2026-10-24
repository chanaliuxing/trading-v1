from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import strategies.qqq_intraday_strategy as qqq_module
from strategies.shared_data import UnderlyingSharedDataPlane
from strategies.stocks_kernel.data.time_utils import ET
from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.qqq_intraday_strategy import QQQIntradayStrategy


@pytest.mark.asyncio
async def test_stocks_kernel_publishes_underlying_to_shared_plane_when_enabled(monkeypatch):
    plane = UnderlyingSharedDataPlane()
    from strategies import shared_data as shared_module
    import strategies.stocks_kernel.strategy as sk_strategy

    monkeypatch.setattr(shared_module, "shared_underlying_data_plane", lambda: plane)
    monkeypatch.setattr(sk_strategy, "shared_underlying_data_plane", lambda: plane)

    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_shared_plane_test_001",
        submit_intent=AsyncMock(return_value={"status": "APPROVED"}),
        subscribe_features=AsyncMock(return_value=None),
        subscribe_tick_data=AsyncMock(return_value=None),
    )
    params = SimpleNamespace(
        strategy_id="kernel_shared_plane_test_001",
        extras={
            "order_namespace": "stkv1",
            "startup_preflight_enabled": False,
            "dry_run": True,
            "data_profile": "qqq_rth_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["session_window", "freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
            "enable_shared_underlying_data_plane": True,
            "shared_underlying_namespace": "qqq_cross_asset",
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    ts_epoch_s = datetime(2026, 4, 10, 10, 0, tzinfo=ET).timestamp()
    strategy._remember_latest_snapshot(
        symbol="QQQ",
        features={"ts_epoch_s": ts_epoch_s, "last_price": 500.0, "vwap": 499.8, "session": "RTH"},
    )
    strategy._publish_shared_underlying_data(
        symbol="QQQ",
        features={"ts_epoch_s": ts_epoch_s, "last_price": 500.0, "vwap": 499.8, "session": "RTH"},
    )

    snap = plane.latest_snapshot(symbol="QQQ", namespace="qqq_cross_asset")
    assert snap is not None
    assert float(snap.last_price or 0.0) == 500.0


def test_shared_plane_readback_consistency_between_qqq_monolith_and_kernel(monkeypatch):
    plane = UnderlyingSharedDataPlane()
    monkeypatch.setattr(qqq_module, "shared_underlying_data_plane", lambda: plane)

    qqq = QQQIntradayStrategy.__new__(QQQIntradayStrategy)
    qqq.params = SimpleNamespace(
        extras={
            "enable_shared_underlying_data_plane": True,
            "shared_underlying_namespace": "qqq_zero_diff",
        }
    )
    qqq.strategy_id = "qqq_intraday_001"
    qqq._qqq_vwap = 501.5
    qqq._qqew_vwap = None
    qqq._publish_shared_underlying_data(symbol="QQQ", ts_ms=1_710_000_000_000, close_px=502.25, vol=1000.0)

    plane.assert_latest_matches(
        symbol="QQQ",
        namespace="qqq_zero_diff",
        expected={"last_price": 502.25, "ts_epoch_s": 1_710_000_000.0},
    )

    ctx = SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="qqq_kernel_shadow_001",
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=SimpleNamespace(extras={}))
    strategy.config = SimpleNamespace(
        raw_extras={
            "enable_shared_underlying_data_plane": True,
            "shared_underlying_namespace": "qqq_zero_diff",
        }
    )
    strategy.data_profile = SimpleNamespace(max_staleness_sec=0.0)

    import strategies.stocks_kernel.strategy as sk_strategy

    monkeypatch.setattr(sk_strategy, "shared_underlying_data_plane", lambda: plane)
    snap = strategy._resolve_runtime_snapshot(symbol="QQQ")

    assert snap is not None
    assert float(snap.last_price or 0.0) == 502.25
    assert float(snap.ts_epoch_s or 0.0) == 1_710_000_000.0
