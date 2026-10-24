import asyncio
from types import SimpleNamespace

import pytest

from strategies.stocks_kernel.strategy import ComposableStocksKernelStrategy
from strategies.stocks_kernel.types import ExitPlan, HistoryBar, PositionLot


def _ctx(submit_calls: list[dict]) -> SimpleNamespace:
    async def _submit_intent(envelope):
        submit_calls.append(
            {
                "symbol": envelope.symbol,
                "extras": dict(envelope.extras),
                "target_qty": float(envelope.target_qty or 0.0),
            }
        )
        return {"success": True, "status": "APPROVED", "order_id": "1", "perm_id": 11}

    return SimpleNamespace(
        log=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
        metrics=SimpleNamespace(),
        engine=SimpleNamespace(),
        strategy_id="kernel_e2e_session_001",
        submit_intent=_submit_intent,
    )


def _bars() -> list[HistoryBar]:
    return [
        HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
        for i in range(1, 16)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session,expected_outside_rth,expected_exchange",
    [
        ("PRE", True, None),
        ("RTH", None, None),
        ("POST", True, None),
        ("OVERNIGHT", True, "OVERNIGHT"),
    ],
)
async def test_stocks_kernel_e2e_session_submit_extras(session, expected_outside_rth, expected_exchange):
    submit_calls: list[dict] = []
    ctx = _ctx(submit_calls)
    params = SimpleNamespace(
        strategy_id="kernel_e2e_session_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 100.0,
            "last_price": 500.0,
            "session": session,
            "vwap": 499.0,
            "trigger_id": "t1",
            "bars_1m": _bars(),
        },
    )
    await asyncio.sleep(0)

    assert len(submit_calls) == 1
    extras = submit_calls[0]["extras"]
    if expected_outside_rth is None:
        assert "outside_rth" not in extras
    else:
        assert bool(extras.get("outside_rth")) is expected_outside_rth
    if expected_exchange is None:
        assert "exchange_override" not in extras
    else:
        assert str(extras.get("exchange_override") or "") == expected_exchange


@pytest.mark.asyncio
async def test_stocks_kernel_e2e_generic_non_qqq_strategy_symbol():
    submit_calls: list[dict] = []
    ctx = _ctx(submit_calls)
    params = SimpleNamespace(
        strategy_id="kernel_e2e_generic_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "IWM", "long_exec_symbol": "IWM", "short_exec_symbol": "IWM"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()

    await strategy.on_features(
        "IWM",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 100.0,
            "last_price": 200.0,
            "session": "RTH",
            "vwap": 199.0,
            "trigger_id": "t1",
            "bars_1m": _bars(),
        },
    )

    assert len(submit_calls) == 1
    assert submit_calls[0]["symbol"] == "IWM"
    assert submit_calls[0]["target_qty"] > 0.0


@pytest.mark.asyncio
async def test_stocks_kernel_e2e_same_symbol_two_strategies_blocks_second_entry_on_shared_inventory():
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    alpha_ctx = _ctx(alpha_calls)
    beta_ctx = _ctx(beta_calls)
    alpha_ctx.strategy_id = "kernel_e2e_alpha_001"
    beta_ctx.strategy_id = "kernel_e2e_beta_001"
    alpha_params = SimpleNamespace(
        strategy_id="kernel_e2e_alpha_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    beta_params = SimpleNamespace(
        strategy_id="kernel_e2e_beta_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    alpha = ComposableStocksKernelStrategy(ctx=alpha_ctx, params=alpha_params)
    beta = ComposableStocksKernelStrategy(ctx=beta_ctx, params=beta_params)
    await alpha.initialize()
    await beta.initialize()
    beta.execution = alpha.execution

    features = {
        "ts_epoch_s": 100.0,
        "now_epoch_s": 100.0,
        "last_price": 500.0,
        "session": "RTH",
        "vwap": 499.0,
        "trigger_id": "t1",
        "bars_1m": _bars(),
    }

    await alpha.on_features("QQQ", dict(features))
    await beta.on_features("QQQ", dict(features))

    assert len(alpha_calls) == 1
    assert len(beta_calls) == 0
    shared_lots = beta.execution.ledger.list_lots(strategy_id=None, symbol="TQQQ", include_closed=False)
    assert len(shared_lots) == 1
    assert shared_lots[0].strategy_id == "kernel_e2e_alpha_001"


@pytest.mark.asyncio
async def test_stocks_kernel_e2e_shared_account_portfolio_limit_blocks_second_strategy():
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    alpha_ctx = _ctx(alpha_calls)
    beta_ctx = _ctx(beta_calls)
    alpha_ctx.strategy_id = "kernel_e2e_portfolio_alpha_001"
    beta_ctx.strategy_id = "kernel_e2e_portfolio_beta_001"
    alpha_params = SimpleNamespace(
        strategy_id="kernel_e2e_portfolio_alpha_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "portfolio_symbol_limit_usd": 150.0,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    beta_params = SimpleNamespace(
        strategy_id="kernel_e2e_portfolio_beta_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "portfolio_symbol_limit_usd": 150.0,
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    alpha = ComposableStocksKernelStrategy(ctx=alpha_ctx, params=alpha_params)
    beta = ComposableStocksKernelStrategy(ctx=beta_ctx, params=beta_params)
    await alpha.initialize()
    await beta.initialize()
    beta.execution = alpha.execution

    alpha.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="alpha-live-lot",
            strategy_id="kernel_e2e_portfolio_alpha_001",
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d-alpha",
            opened_by_trigger_id="t-alpha",
            entry_order_ref="stkv1:kernel_e2e_portfolio_alpha_001:TQQQ:ENTRY:LOT:alpha-live-lot",
            live_qty=4.0,
            filled_qty=4.0,
            avg_entry_price=50.0,
            state="live",
        )
    )

    await beta.on_features(
        "QQQ",
        {
            "ts_epoch_s": 100.0,
            "now_epoch_s": 100.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "trigger_id": "t1",
            "bars_1m": _bars(),
        },
    )

    assert len(alpha_calls) == 0
    assert len(beta_calls) == 0


@pytest.mark.asyncio
async def test_stocks_kernel_e2e_same_symbol_two_strategies_lane_scope_allows_parallel_entry():
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    alpha_ctx = _ctx(alpha_calls)
    beta_ctx = _ctx(beta_calls)
    alpha_ctx.strategy_id = "kernel_e2e_lane_alpha_001"
    beta_ctx.strategy_id = "kernel_e2e_lane_beta_001"
    alpha_params = SimpleNamespace(
        strategy_id="kernel_e2e_lane_alpha_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [
                {
                    "signal_symbol": "QQQ",
                    "long_exec_symbol": "TQQQ",
                    "short_exec_symbol": "SQQQ",
                    "execution_lanes": [
                        {"lane_id": "alpha_long", "symbol": "TQQQ", "side": "LONG", "role": "execution", "ownership_scope": "lane"},
                        {"lane_id": "alpha_short", "symbol": "SQQQ", "side": "SHORT", "role": "execution", "ownership_scope": "lane"},
                    ],
                }
            ],
        },
    )
    beta_params = SimpleNamespace(
        strategy_id="kernel_e2e_lane_beta_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [
                {
                    "signal_symbol": "QQQ",
                    "long_exec_symbol": "TQQQ",
                    "short_exec_symbol": "SQQQ",
                    "execution_lanes": [
                        {"lane_id": "beta_long", "symbol": "TQQQ", "side": "LONG", "role": "execution", "ownership_scope": "lane"},
                        {"lane_id": "beta_short", "symbol": "SQQQ", "side": "SHORT", "role": "execution", "ownership_scope": "lane"},
                    ],
                }
            ],
        },
    )
    alpha = ComposableStocksKernelStrategy(ctx=alpha_ctx, params=alpha_params)
    beta = ComposableStocksKernelStrategy(ctx=beta_ctx, params=beta_params)
    await alpha.initialize()
    await beta.initialize()
    beta.execution = alpha.execution

    features = {
        "ts_epoch_s": 100.0,
        "now_epoch_s": 100.0,
        "last_price": 500.0,
        "session": "RTH",
        "vwap": 499.0,
        "atr_eff": 1.0,
        "v3m": 0.1,
        "trigger_id": "t1",
        "bars_1m": _bars(),
    }

    await alpha.on_features("QQQ", dict(features))
    await beta.on_features("QQQ", dict(features))

    assert len(alpha_calls) == 1
    assert len(beta_calls) == 1
    shared_lots = beta.execution.ledger.list_lots(strategy_id=None, symbol="TQQQ", include_closed=False)
    assert len(shared_lots) == 2
    assert {lot.strategy_id for lot in shared_lots} == {"kernel_e2e_lane_alpha_001", "kernel_e2e_lane_beta_001"}
    assert all(str(getattr(lot, "ownership_scope", "") or "") == "lane" for lot in shared_lots)


@pytest.mark.asyncio
async def test_stocks_kernel_e2e_same_symbol_authority_only_owner_can_flatten():
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    alpha_ctx = _ctx(alpha_calls)
    beta_ctx = _ctx(beta_calls)
    alpha_ctx.strategy_id = "kernel_e2e_auth_alpha_001"
    beta_ctx.strategy_id = "kernel_e2e_auth_beta_001"
    params = SimpleNamespace(
        strategy_id="kernel_e2e_auth_alpha_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    alpha = ComposableStocksKernelStrategy(ctx=alpha_ctx, params=params)
    beta = ComposableStocksKernelStrategy(ctx=beta_ctx, params=SimpleNamespace(**{**params.__dict__, "strategy_id": "kernel_e2e_auth_beta_001"}))
    await alpha.initialize()
    await beta.initialize()
    beta.execution = alpha.execution

    lot = PositionLot(
        position_lot_id="lot-auth-flat-1",
        strategy_id="kernel_e2e_auth_alpha_001",
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d-alpha",
        opened_by_trigger_id="t-alpha",
        entry_order_ref="stkv1:kernel_e2e_auth_alpha_001:TQQQ:ENTRY:LOT:lot-auth-flat-1",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=50.0,
        state="live",
        flatten_authority="strategy",
        protection_authority="strategy",
        adopt_authority="strategy",
    )
    alpha.execution.ledger.open_lot(lot)
    snapshot = SimpleNamespace(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")

    await beta._submit_exit_intent(
        exit_plan=ExitPlan(
            symbol="TQQQ",
            order_role="FLATTEN",
            qty=2.0,
            position_lot_id=lot.position_lot_id,
            payload={"reason": "cross_strategy_force_flatten"},
        ),
        snapshot=snapshot,
        lot=lot,
    )
    assert len(beta_calls) == 0

    await alpha._submit_exit_intent(
        exit_plan=ExitPlan(
            symbol="TQQQ",
            order_role="FLATTEN",
            qty=2.0,
            position_lot_id=lot.position_lot_id,
            payload={"reason": "owner_flatten"},
        ),
        snapshot=snapshot,
        lot=lot,
    )
    assert len(alpha_calls) == 1


@pytest.mark.asyncio
async def test_stocks_kernel_e2e_same_symbol_authority_only_owner_can_rebuild_protection():
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    alpha_ctx = _ctx(alpha_calls)
    beta_ctx = _ctx(beta_calls)
    alpha_ctx.strategy_id = "kernel_e2e_auth2_alpha_001"
    beta_ctx.strategy_id = "kernel_e2e_auth2_beta_001"
    params = SimpleNamespace(
        strategy_id="kernel_e2e_auth2_alpha_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    alpha = ComposableStocksKernelStrategy(ctx=alpha_ctx, params=params)
    beta = ComposableStocksKernelStrategy(ctx=beta_ctx, params=SimpleNamespace(**{**params.__dict__, "strategy_id": "kernel_e2e_auth2_beta_001"}))
    await alpha.initialize()
    await beta.initialize()
    beta.execution = alpha.execution

    lot = PositionLot(
        position_lot_id="lot-auth-stop-1",
        strategy_id="kernel_e2e_auth2_alpha_001",
        symbol="TQQQ",
        side="LONG",
        opened_by_decision_id="d-alpha",
        opened_by_trigger_id="t-alpha",
        entry_order_ref="stkv1:kernel_e2e_auth2_alpha_001:TQQQ:ENTRY:LOT:lot-auth-stop-1",
        live_qty=2.0,
        filled_qty=2.0,
        avg_entry_price=50.0,
        state="live",
        flatten_authority="strategy",
        protection_authority="strategy",
        adopt_authority="strategy",
        protection_order_refs=[],
        extras={
            "protective_oco": {"sl_offset_abs": 1.0, "tp_offset_abs": 2.0},
            "protection_state": {"position_lifecycle": "live", "protection_lifecycle": "repair_pending"},
        },
    )
    alpha.execution.ledger.open_lot(lot)
    snapshot = SimpleNamespace(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")

    rebuilt = await beta._attempt_rebuild_missing_protection_for_lot(
        lot=lot,
        snapshot=snapshot,
        reason="cross_strategy_rebuild",
    )
    assert rebuilt is False
    assert len(beta_calls) == 0

    rebuilt = await alpha._attempt_rebuild_missing_protection_for_lot(
        lot=lot,
        snapshot=snapshot,
        reason="owner_rebuild",
    )
    assert rebuilt is True
    assert len(alpha_calls) == 1


@pytest.mark.asyncio
async def test_stocks_kernel_e2e_position_reconcile_respects_adopt_authority_template():
    submit_calls: list[dict] = []
    ctx = _ctx(submit_calls)
    ctx.strategy_id = "kernel_e2e_adopt_auth_001"
    params = SimpleNamespace(
        strategy_id="kernel_e2e_adopt_auth_001",
        capital_limit_usd=10_000,
        ttl_ms=5_000,
        extras={
            "dry_run": False,
            "startup_preflight_enabled": False,
            "data_profile": "equity_offhours_standard",
            "policy_id": "qqq_intraday_policy_v1",
            "feature_set": ["vwap", "atr_eff", "v3m"],
            "gate_pipeline": ["freshness", "seed_ready", "direction_match", "data_integrity"],
            "universe": [{"signal_symbol": "QQQ", "long_exec_symbol": "TQQQ", "short_exec_symbol": "SQQQ"}],
        },
    )
    strategy = ComposableStocksKernelStrategy(ctx=ctx, params=params)
    await strategy.initialize()
    strategy.execution.ledger.open_lot(
        PositionLot(
            position_lot_id="lot-adopt-auth-1",
            strategy_id=strategy.strategy_id,
            symbol="TQQQ",
            side="LONG",
            opened_by_decision_id="d1",
            opened_by_trigger_id="t1",
            entry_order_ref="stkv1:kernel_e2e_adopt_auth_001:TQQQ:ENTRY:LOT:lot-adopt-auth-1",
            live_qty=2.0,
            filled_qty=2.0,
            avg_entry_price=50.0,
            state="live",
            adopt_authority="portfolio",
        )
    )

    await strategy.on_position_update(
        SimpleNamespace(
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=49.9,
            realized_pnl_usd=0.0,
            unrealized_pnl_usd=0.0,
        )
    )

    lot = strategy.execution.ledger.get_lot("lot-adopt-auth-1")
    assert lot is not None
    assert lot.live_qty == pytest.approx(2.0)
