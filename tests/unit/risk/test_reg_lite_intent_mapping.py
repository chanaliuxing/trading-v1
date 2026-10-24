import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.risk.reg_lite import RegLiteGuard, MarketSnap, intent_from_decision_envelope


def test_safety_negative_target_maps_to_sell_not_short():
    env = {
        "decision_id": "d1",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "target_qty": -138.0,
        "order_style": {"kind": "STP", "stop_px": 49.85},
        "extras": {"protective_stop": True, "close_only": True},
    }
    intent = intent_from_decision_envelope(env)
    assert intent.side == "SELL"
    assert intent.qty == 138.0


def test_non_safety_negative_target_still_maps_to_sell_short():
    env = {
        "decision_id": "d2",
        "strategy_id": "some_short_strategy",
        "symbol": "TQQQ",
        "target_qty": -10.0,
        "order_style": {"kind": "MKT"},
        "extras": {},
    }
    intent = intent_from_decision_envelope(env)
    assert intent.side == "SELL_SHORT"
    assert intent.qty == 10.0


def test_ssr_does_not_block_safety_sell_as_short():
    env = {
        "decision_id": "d3",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "target_qty": -50.0,
        "order_style": {"kind": "MKT"},
        "extras": {"flatten_action": True, "close_only": True},
    }
    intent = intent_from_decision_envelope(env)
    guard = RegLiteGuard(enable_ssr_strict=True)
    snap = MarketSnap(
        bid_px=50.10,
        ask_px=50.12,
        mid_px=50.11,
        last_px=50.11,
        ssr_active=True,
        halted=False,
    )
    hits = guard.check(intent, snap)
    assert all(h.rule_id != "ssr" for h in hits)


def test_option_lot_size_tolerates_float_noise_near_integer_contracts():
    env = {
        "decision_id": "d4",
        "strategy_id": "qqq_0dte_opt_live_001",
        "symbol": "OPT:QQQ:20260325:585:C",
        "target_qty": 2.9999999999999996,
        "order_style": {"kind": "MKT"},
        "extras": {
            "instrument_type": "OPTION",
            "opt_bid_px": 2.92,
            "opt_ask_px": 2.95,
            "opt_mid_px": 2.935,
            "opt_tick_size": 0.01,
            "opt_lot_size": 1.0,
        },
    }
    intent = intent_from_decision_envelope(env)
    guard = RegLiteGuard()
    snap = MarketSnap(
        bid_px=2.92,
        ask_px=2.95,
        mid_px=2.935,
        last_px=2.94,
        tick_size=0.01,
        lot_size=1.0,
        halted=False,
    )
    hits = guard.check(intent, snap)
    assert all(h.rule_id != "lot_size" for h in hits)
