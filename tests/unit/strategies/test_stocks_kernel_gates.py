import pytest

from strategies.stocks_kernel.gates.data_integrity import DataIntegrityGate
from strategies.stocks_kernel.gates.direction import DirectionMatchGate
from strategies.stocks_kernel.gates.freshness import FreshnessGate
from strategies.stocks_kernel.gates.base import GateRuntimeContext
from strategies.stocks_kernel.gates.pipeline import GatePipeline
from strategies.stocks_kernel.gates.registry import GateRegistry, default_gate_registry
from strategies.stocks_kernel.gates.seed_ready import SeedReadyGate
from strategies.stocks_kernel.gates.session_window import SessionWindowGate
from strategies.stocks_kernel.data.contexts import build_data_context_bundle
from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, HistoryBundle, MarketSnapshot


def _ctx(session: str = "RTH", direction_value: float = 0.02) -> FeatureContext:
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session=session)
    return FeatureContext(
        symbol="QQQ",
        snapshot=snapshot,
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": direction_value, "atr_eff": 1.2},
        readiness={"v3m": "ready", "atr_eff": "ready"},
    )


def _ready() -> DataReadinessReport:
    return DataReadinessReport(
        ready=True,
        reason="ready",
        freshness_ok=True,
        history_ready=True,
        snapshot_ready=True,
    )


def test_gate_pipeline_blocks_on_first_failing_gate():
    pipeline = GatePipeline(
        [
            SessionWindowGate(allowed_sessions=["RTH"]),
            FreshnessGate(),
            SeedReadyGate(required_features=["atr_eff"]),
            DirectionMatchGate(feature_id="v3m", min_abs_value=0.05),
            DataIntegrityGate(),
        ]
    )

    report = pipeline.evaluate(symbol="QQQ", feature_context=_ctx(direction_value=0.02), readiness=_ready())

    assert report.allowed is False
    assert report.blocked_by == "direction_match"
    assert len(report.decisions) == 4


def test_gate_pipeline_allows_happy_path():
    pipeline = GatePipeline(
        [
            SessionWindowGate(allowed_sessions=["RTH"]),
            FreshnessGate(),
            SeedReadyGate(required_features=["atr_eff"]),
            DirectionMatchGate(feature_id="v3m", min_abs_value=0.01),
            DataIntegrityGate(),
        ]
    )

    report = pipeline.evaluate(symbol="QQQ", feature_context=_ctx(direction_value=0.02), readiness=_ready())

    assert report.allowed is True
    assert report.blocked_by is None
    assert [item.gate_id for item in report.decisions] == [
        "session_window",
        "freshness",
        "seed_ready",
        "direction_match",
        "data_integrity",
    ]


def test_gate_registry_builds_parameterized_gate():
    registry = default_gate_registry()
    gate = registry.build("direction_match", config={"feature_id": "v3m", "min_abs_value": 0.05})

    decision = gate.evaluate(symbol="QQQ", feature_context=_ctx(direction_value=0.02), readiness=_ready())

    assert decision.gate_id == "direction_match"
    assert decision.allowed is False
    assert decision.reason == "direction_below_threshold"


def test_open_orders_isolation_gate_blocks_when_pending_orders_present():
    registry = default_gate_registry()
    gate = registry.build("open_orders_isolation", config={})
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH", extras={"same_strategy_pending_orders": 2})
    ctx = FeatureContext(symbol="QQQ", snapshot=snapshot, history=HistoryBundle(symbol="QQQ"), values={}, readiness={})
    decision = gate.evaluate(symbol="QQQ", feature_context=ctx, readiness=_ready())
    assert decision.allowed is False
    assert decision.trader_reason_code == "open_orders_isolation"


def test_foreign_position_isolation_gate_blocks_on_nonzero_foreign_qty():
    registry = default_gate_registry()
    gate = registry.build("foreign_position_isolation", config={})
    snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH", extras={"foreign_position_qty": 1e-6})
    ctx = FeatureContext(symbol="QQQ", snapshot=snapshot, history=HistoryBundle(symbol="QQQ"), values={}, readiness={})
    decision = gate.evaluate(symbol="QQQ", feature_context=ctx, readiness=_ready())
    assert decision.allowed is False
    assert decision.trader_reason_code == "foreign_position_isolation"


def test_multi_scope_readiness_gate_allows_when_runtime_context_missing_by_default():
    registry = default_gate_registry()
    gate = registry.build("multi_scope_readiness", config={"execution_required": True})

    decision = gate.evaluate(symbol="QQQ", feature_context=_ctx(), readiness=_ready())

    assert decision.allowed is True
    assert decision.reason == "multi_scope_runtime_context_missing"


def test_multi_scope_readiness_gate_blocks_when_execution_scope_not_ready():
    registry = default_gate_registry()
    gate = registry.build("multi_scope_readiness", config={"execution_required": True, "allow_missing_runtime_context": False})
    signal_snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")
    ctx = _ctx()
    bundle = build_data_context_bundle(
        signal_symbol="QQQ",
        signal_snapshot=signal_snapshot,
        signal_history=HistoryBundle(symbol="QQQ"),
        signal_readiness=_ready(),
        signal_feature_context=ctx,
        exec_contexts={"TQQQ": {"snapshot": MarketSnapshot(symbol="TQQQ", ts_epoch_s=100.0, last_price=50.0), "readiness": DataReadinessReport(ready=False, reason="snapshot_stale", freshness_ok=False, history_ready=True, snapshot_ready=True)}},
    )
    ctx.values["__data_context_bundle__"] = bundle

    decision = gate.evaluate(symbol="QQQ", feature_context=ctx, readiness=_ready())

    assert decision.allowed is False
    assert decision.reason == "multi_scope_not_ready"


def test_multi_scope_readiness_gate_allows_when_execution_scope_ready():
    registry = default_gate_registry()
    gate = registry.build("multi_scope_readiness", config={"execution_required": True, "allow_missing_runtime_context": False})
    signal_snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")
    ctx = _ctx()
    bundle = build_data_context_bundle(
        signal_symbol="QQQ",
        signal_snapshot=signal_snapshot,
        signal_history=HistoryBundle(symbol="QQQ"),
        signal_readiness=_ready(),
        signal_feature_context=ctx,
        exec_contexts={"TQQQ": {"snapshot": MarketSnapshot(symbol="TQQQ", ts_epoch_s=100.0, last_price=50.0), "readiness": _ready()}},
    )
    ctx.values["__data_context_bundle__"] = bundle

    decision = gate.evaluate(symbol="QQQ", feature_context=ctx, readiness=_ready())

    assert decision.allowed is True
    assert decision.reason == "ready"


def test_multi_scope_readiness_gate_consumes_runtime_context_contract():
    registry = default_gate_registry()
    gate = registry.build("multi_scope_readiness", config={"execution_required": True, "allow_missing_runtime_context": False})
    signal_snapshot = MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH")
    ctx = _ctx()
    bundle = build_data_context_bundle(
        signal_symbol="QQQ",
        signal_snapshot=signal_snapshot,
        signal_history=HistoryBundle(symbol="QQQ"),
        signal_readiness=_ready(),
        signal_feature_context=ctx,
        exec_contexts={"TQQQ": {"snapshot": MarketSnapshot(symbol="TQQQ", ts_epoch_s=100.0, last_price=50.0), "readiness": _ready()}},
    )

    decision = gate.evaluate(
        symbol="QQQ",
        feature_context=ctx,
        readiness=_ready(),
        runtime=GateRuntimeContext(data_context_bundle=bundle),
    )

    assert decision.allowed is True
    assert decision.reason == "ready"


def test_gate_pipeline_does_not_silently_fallback_when_runtime_gate_raises_type_error():
    class _RuntimeBugGate:
        gate_id = "runtime_bug_gate"

        def evaluate(self, *, symbol, feature_context, readiness, runtime=None):
            _ = (symbol, feature_context, readiness)
            if runtime is not None:
                raise TypeError("runtime internal bug")
            from strategies.stocks_kernel.types import GateDecision

            return GateDecision(
                gate_id=self.gate_id,
                allowed=True,
                reason="unexpected_fallback",
                trader_reason_code="unexpected_fallback",
                payload={},
            )

    pipeline = GatePipeline([_RuntimeBugGate()])
    report = pipeline.evaluate(
        symbol="QQQ",
        feature_context=_ctx(),
        readiness=_ready(),
        runtime=GateRuntimeContext(data_context_bundle=object()),
    )

    assert report.allowed is False
    assert report.blocked_by == "runtime_bug_gate"


def test_gate_registry_register_from_specs_supports_dynamic_class_loading():
    registry = GateRegistry()
    registry.register_from_specs(
        [
            {
                "gate_id": "dynamic_session_window",
                "module": "strategies.stocks_kernel.gates.session_window",
                "attr": "SessionWindowGate",
                "default_config": {"allowed_sessions": ["RTH"]},
            }
        ]
    )

    gate = registry.build("dynamic_session_window", config={})
    decision = gate.evaluate(symbol="QQQ", feature_context=_ctx(session="RTH"), readiness=_ready())

    assert decision.allowed is True
    assert decision.gate_id == "session_window"


def test_gate_registry_register_from_specs_rejects_non_gate_class():
    registry = GateRegistry()

    with pytest.raises(TypeError, match="must subclass Gate"):
        registry.register_from_specs(
            [
                {
                    "gate_id": "bad_gate",
                    "module": "types",
                    "attr": "SimpleNamespace",
                }
            ]
        )
