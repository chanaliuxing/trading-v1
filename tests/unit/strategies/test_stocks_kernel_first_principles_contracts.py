from types import SimpleNamespace
from pathlib import Path

import pytest

from strategies.stocks_kernel.config_loader import load_kernel_config
from strategies.stocks_kernel.execution.order_identity import (
    build_order_ref,
    build_protection_order_ref,
    order_ref_belongs_to_strategy,
    parse_order_ref,
    parse_protection_order_ref,
)
from strategies.stocks_kernel.runtime.evaluator import StrategyEvaluator
from strategies.stocks_kernel.runtime.orchestrator import RuntimeOrchestrator, RuntimeStageSpec
from strategies.stocks_kernel.runtime.stage_registry import RuntimeStageRegistry
from strategies.stocks_kernel.features.registry import default_feature_registry
from strategies.stocks_kernel.gates import GatePipeline
from strategies.stocks_kernel.policies.qqq_intraday_policy_v1 import QQQIntradayPolicyV1
from strategies.stocks_kernel.data.profiles import get_data_profile
from strategies.stocks_kernel.types import HistoryBar, HistoryBundle


class _HistoryProvider:
    async def load(self, *, symbol: str, resolution: str, limit: int):
        _ = (resolution, limit)
        return HistoryBundle(
            symbol=symbol,
            bars_1m=tuple(
                HistoryBar(ts_epoch_s=float(i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=10 * i)
                for i in range(1, 16)
            ),
        )


def test_order_ref_supports_optional_lane_id():
    order_ref = build_order_ref(
        namespace="stkv1",
        strategy_id="alpha",
        symbol="SOXL",
        kind="ENTRY",
        lane_id="trend_lane",
        trigger_id="t1",
        lot_id="lot1",
    )

    parsed = parse_order_ref(order_ref)

    assert parsed is not None
    assert parsed.lane_id == "trend_lane"
    assert parsed.trigger_id == "t1"


def test_protection_order_ref_has_explicit_owner_identity():
    order_ref = build_protection_order_ref(
        role="sl",
        account_id="DU123",
        strategy_id="alpha",
        symbol="NVDA",
        suffix="lot1",
    )

    parsed = parse_protection_order_ref(order_ref)

    assert order_ref == "sl:DU123|alpha|NVDA|lot1"
    assert parsed is not None
    assert parsed.role == "SL"
    assert parsed.account_id == "DU123"
    assert parsed.strategy_id == "alpha"
    assert parsed.symbol == "NVDA"
    assert parsed.suffix == "lot1"
    assert order_ref_belongs_to_strategy(order_ref, namespace="stkv1", strategy_id="alpha") is True
    assert order_ref_belongs_to_strategy(order_ref, namespace="stkv1", strategy_id="beta") is False


def test_malformed_protection_order_ref_has_no_owner_identity():
    assert parse_protection_order_ref("sl:NVDA") is None
    assert parse_protection_order_ref("sl:DU123|alpha|") is None
    assert order_ref_belongs_to_strategy("sl:NVDA", namespace="stkv1", strategy_id="alpha") is False


def test_config_loader_parses_extended_lane_authority_fields():
    params = SimpleNamespace(
        extras={
            "universe": [
                {
                    "signal_symbol": "QQQ",
                    "execution_lanes": [
                        {
                            "lane_id": "fast_long",
                            "symbol": "TQQQ",
                            "side": "LONG",
                            "flatten_authority": "portfolio",
                            "protection_authority": "strategy",
                            "ownership_scope": "lane",
                        }
                    ],
                    "signal_lanes": [{"lane_id": "signal_main", "symbol": "QQQ"}],
                    "ownership_scope": "lane",
                    "strategy_namespace": "qqq_stack",
                }
            ]
        }
    )

    config = load_kernel_config("alpha", params)
    instrument = config.instruments[0]

    assert instrument.strategy_namespace == "qqq_stack"
    assert instrument.ownership_scope == "lane"
    assert instrument.execution_lanes[0].flatten_authority == "portfolio"
    assert instrument.signal_lanes[0].lane_id == "signal_main"


async def test_evaluator_exposes_data_context_bundle_and_action_candidates():
    evaluator = StrategyEvaluator(
        feature_registry=default_feature_registry(),
        gate_pipeline=GatePipeline([]),
        policy=QQQIntradayPolicyV1(),
        data_profile=get_data_profile("qqq_rth_standard"),
        history_provider=_HistoryProvider(),
        feature_ids=["vwap", "atr_eff", "v3m"],
    )

    bundle = await evaluator.evaluate(
        symbol="QQQ",
        features={
            "ts_epoch_s": 100.0,
            "now_epoch_s": 101.0,
            "last_price": 500.0,
            "session": "RTH",
            "vwap": 499.0,
            "planned_qty": 2,
            "trigger_id": "t1",
        },
    )

    assert bundle.data_context_bundle.signal.scope == "signal"
    assert bundle.action_candidates
    assert bundle.winning_actions == bundle.action_plans
    assert bundle.blocked_actions == ()


def test_runtime_orchestrator_produces_default_runtime_graph():
    plan = RuntimeOrchestrator().build_plan()

    assert [stage.stage_id for stage in plan.stages][:3] == [
        "runtime_recovery",
        "broker_truth_sync",
        "data_evaluation",
    ]


def test_runtime_orchestrator_respects_disabled_stage_overrides():
    orchestrator = RuntimeOrchestrator(
        stage_overrides={
            "safety_invariants": {"enabled": False},
            "entry_submission": {"enabled": True, "mode": "default"},
        }
    )

    assert "safety_invariants" in orchestrator.iter_enabled_stage_ids()
    safety_stage = orchestrator.build_plan().stage("safety_invariants")
    assert safety_stage is not None
    assert safety_stage.metadata.get("enabled_clamped") is True
    assert orchestrator.build_plan().stage("entry_submission").metadata["mode"] == "default"


def test_runtime_orchestrator_supports_registered_extra_stage_specs():
    orchestrator = RuntimeOrchestrator(
        stage_specs=(
            *RuntimeOrchestrator.default_stage_specs(),
            RuntimeStageSpec(stage_id="custom_post_eval", order=5, metadata={"handler": "_runtime_stage_data_evaluation"}),
        )
    )
    plan = orchestrator.build_plan()
    assert plan.stage("custom_post_eval") is not None


def test_runtime_orchestrator_clamps_reserved_stage_order_chain():
    orchestrator = RuntimeOrchestrator(
        stage_overrides={
            "entry_submission": {"order": 1},
            "runtime_recovery": {"order": 9},
        }
    )
    ids = [stage.stage_id for stage in orchestrator.build_plan().stages]
    assert ids.index("runtime_recovery") < ids.index("entry_submission")


def test_runtime_orchestrator_auto_inserts_missing_reserved_stage_specs():
    orchestrator = RuntimeOrchestrator(
        stage_specs=(RuntimeStageSpec(stage_id="data_evaluation", order=100),),
    )
    plan = orchestrator.build_plan()
    for stage_id in RuntimeOrchestrator.RESERVED_ORDER_CHAIN:
        stage = plan.stage(stage_id)
        assert stage is not None
        assert stage.enabled is True


def test_runtime_stage_registry_register_from_specs_fails_fast_on_missing_module_attr():
    registry = RuntimeStageRegistry()

    with pytest.raises(ValueError, match="requires module/attr or strategy_handler_name"):
        registry.register_from_specs([{"stage_id": "bad_stage", "order": 10}])


def test_strategy_core_uses_assembly_layer_instead_of_default_pack_directly():
    strategy_path = Path("./strategies/stocks_kernel/strategy.py")
    source = strategy_path.read_text(encoding="utf-8")

    assert "build_default_kernel_assembly" not in source
    assert "legacy_kernel_assembly_provider" in source
    assert "default_gate_registry()" not in source
    assert "default_policy_registry()" not in source
    assert "default_runtime_stage_registry()" not in source
