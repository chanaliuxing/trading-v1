from strategies.options_kernel.parity import OptionParityRunner
from libs.strategy.base import DecisionEnvelope, Freshness, OrderStyle


def _env(*, decision_id: str, extras: dict) -> DecisionEnvelope:
    return DecisionEnvelope(
        decision_id=decision_id,
        strategy_id="s1",
        symbol="OPT:SPY:20260331:635:P",
        ts_bar_end_us=1,
        target_qty=4.0,
        freshness=Freshness(),
        order_style=OrderStyle(kind="MKT"),
        extras=extras,
    )


def test_option_parity_ignores_definition_namespace_extras() -> None:
    old = _env(
        decision_id="d1",
        extras={"risk_symbol": "OPT:SPY:1DTE", "ref_px": 3.2},
    )
    new = _env(
        decision_id="d2",
        extras={
            "risk_symbol": "OPT:SPY:1DTE",
            "ref_px": 3.2,
            "definition_id": "SPY_MA_CROSS_STACK_CONT_V2_DUAL",
            "audit_namespace": "spy_ma_option",
            "position_namespace": "optk.spy_ma_cross_stack_cont_v2_dual",
        },
    )
    report = OptionParityRunner().compare_envelopes([old], [new])
    assert report["matched"] is True
