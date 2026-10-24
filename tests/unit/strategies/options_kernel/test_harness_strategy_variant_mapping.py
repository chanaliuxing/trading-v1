from strategies.options_kernel.harness import _resolve_strategy_classes
from strategies.spy_1dte_ma10_reject_live_strategy import SPY1DTEMA10RejectLiveStrategy


def test_resolve_strategy_classes_uses_spy_legacy_for_spy_variant() -> None:
    old_cls, _, old_strategy_id, _ = _resolve_strategy_classes({"strategy_variant": "spy_option_ma"})
    assert old_cls is SPY1DTEMA10RejectLiveStrategy
    assert old_strategy_id == "spy_1dte_ma10_reject_legacy_harness"
