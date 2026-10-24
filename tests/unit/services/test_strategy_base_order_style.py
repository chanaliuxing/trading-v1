from libs.strategy.base import validate_order_style
from services.strategy.strategy_base import DecisionEnvelope, Freshness, OrderStyle


def test_services_strategy_order_style_serializes_trailing_fields() -> None:
    envelope = DecisionEnvelope(
        decision_id="d-trail",
        strategy_id="s1",
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=-3,
        freshness=Freshness(ttl_ms=1000),
        order_style=OrderStyle(
            kind="TRAIL_LIMIT",
            trail_percent=1.5,
            trail_stop_px=102.12,
            lmt_price_offset=0.05,
            protective_stop=True,
            close_only=True,
            reduce_only=True,
            role="runner_trail",
            outside_rth=False,
        ),
    )

    style = envelope.to_dict()["order_style"]

    assert style["kind"] == "TRAIL_LIMIT"
    assert style["trail_percent"] == 1.5
    assert style["trail_stop_px"] == 102.12
    assert style["lmt_price_offset"] == 0.05
    assert style["protective_stop"] is True
    assert style["close_only"] is True
    assert style["reduce_only"] is True
    assert style["role"] == "runner_trail"
    assert style["outside_rth"] is False


def test_services_strategy_order_style_is_compatible_with_shared_validator() -> None:
    validate_order_style(
        OrderStyle(
            kind="TRAIL",
            trail_amount=0.35,
            trail_stop_px=102.12,
            protective_stop=True,
            close_only=True,
        )
    )
