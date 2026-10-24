from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from libs.strategy import StrategyContext, StrategyParams
from strategies.qqq_intraday_strategy import QQQIntradayStrategy


ROOT = Path(__file__).resolve().parents[3]


def _make_qqq_strategy():
    ctx = MagicMock(spec=StrategyContext)
    ctx.log = MagicMock()
    ctx.metrics = lambda *args, **kwargs: None
    ctx.strategy_id = "qqq_intraday_001"
    ctx.engine = MagicMock()
    ctx.position = AsyncMock(return_value=None)
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    params = StrategyParams()
    params.extras = {
        "enable_observability_file": False,
        "fail_fast_enabled": False,
        "submit_exchange_stop_intent": True,
    }
    return QQQIntradayStrategy(ctx, params)


def test_index_super_win_runtime_v2_sources_do_not_reference_qqqv1_namespace():
    paths = [
        ROOT / "strategies" / "index_super_win_strategy.py",
        ROOT / "strategies" / "index_super_win_components.py",
        ROOT / "services" / "strategy_runtime_v2" / "__init__.py",
        ROOT / "services" / "strategy_runtime_v2" / "broker_snapshot_service.py",
        ROOT / "services" / "strategy_runtime_v2" / "coordinator.py",
        ROOT / "services" / "strategy_runtime_v2" / "order_identity.py",
        ROOT / "services" / "strategy_runtime_v2" / "ownership_service.py",
        ROOT / "services" / "strategy_runtime_v2" / "runtime_store.py",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "qqqv1:" not in text, f"unexpected qqqv1 namespace reference in {path}"


def test_index_super_win_config_freezes_isolated_runtime_contract():
    text = (ROOT / "config.yml").read_text(encoding="utf-8")

    assert "- id: index_super_win_001" in text
    assert "runtime_scope: index_super_win" in text
    assert "order_namespace: iswv2" in text
    assert "entry_isolation:\n    enabled: true\n    mode: strategy_only\n    symbols: []" in text
    assert "diag_log_path: runtime/state/index_super_win_diag_index_super_win_001.jsonl" in text
    assert "diag_snapshot_path: runtime/state/index_super_win_diag_snapshot_index_super_win_001.json" in text
    assert "diag_log_path: runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl" in text


def test_index_manifest_module_exists_for_config_only_symbol_rollout():
    text = (ROOT / "strategies" / "index_super_win_manifest.py").read_text(encoding="utf-8")

    assert "class ReplicaSymbolManifest" in text
    assert "class ReplicaManifest" in text
    assert "replica_manifests" in text


def test_qqq_order_matching_rejects_iswv2_namespace_for_same_symbol():
    strategy = _make_qqq_strategy()
    order = SimpleNamespace(
        symbol="TQQQ",
        strategy_id="",
        order_ref="iswv2:index_super_win_001:TQQQ:ENTRY:TRG:abc123",
    )

    assert strategy._is_order_for_strategy_symbol(order, "TQQQ") is False


@pytest.mark.asyncio
async def test_qqq_cancel_cleanup_skips_iswv2_orders_on_same_symbol():
    strategy = _make_qqq_strategy()
    foreign_order = SimpleNamespace(
        strategy_id="index_super_win_001",
        symbol="TQQQ",
        state="SUBMITTED",
        order_id="91",
        order_ref="iswv2:index_super_win_001:TQQQ:ENTRY:TRG:foreign",
    )
    own_order = SimpleNamespace(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        state="SUBMITTED",
        order_id="92",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:mine",
    )
    strategy.ctx.open_orders = AsyncMock(return_value=[foreign_order, own_order])
    cancel_calls: list[int] = []

    async def _cancel(order_id: int):
        cancel_calls.append(int(order_id))
        return True

    strategy._resolve_broker_cancel_fn = MagicMock(return_value=_cancel)

    cancelled = await strategy._cancel_strategy_open_orders_best_effort(
        reason="unit_test_cleanup",
        symbol="TQQQ",
    )

    assert cancelled == 1
    assert cancel_calls == [92]
