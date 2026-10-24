from strategies.stocks_kernel.gates.foreign_position_isolation import ForeignPositionIsolationGate
from strategies.stocks_kernel.gates.open_orders_isolation import OpenOrdersIsolationGate
from strategies.stocks_kernel.gates.risk_budget import RiskBudgetGate
from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, HistoryBundle, MarketSnapshot


def _ctx(**extras) -> FeatureContext:
    return FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=100.0, last_price=500.0, session="RTH", extras=extras),
        history=HistoryBundle(symbol="QQQ"),
        values={},
        readiness={},
    )


def _ready() -> DataReadinessReport:
    return DataReadinessReport(ready=True, reason="ready", freshness_ok=True, history_ready=True, snapshot_ready=True)


def test_risk_budget_gate_blocks_when_notional_exceeds_limit():
    gate = RiskBudgetGate(max_notional=1000.0)
    decision = gate.evaluate(symbol="QQQ", feature_context=_ctx(current_notional=600.0, planned_notional=500.0), readiness=_ready())
    assert decision.allowed is False
    assert decision.reason == "risk_budget_exceeded"


def test_open_orders_and_foreign_position_isolation_gates():
    open_gate = OpenOrdersIsolationGate()
    foreign_gate = ForeignPositionIsolationGate()
    open_decision = open_gate.evaluate(symbol="QQQ", feature_context=_ctx(same_strategy_pending_orders=1), readiness=_ready())
    foreign_decision = foreign_gate.evaluate(symbol="QQQ", feature_context=_ctx(foreign_position_qty=100), readiness=_ready())
    assert open_decision.allowed is False
    assert foreign_decision.allowed is False
