from strategies.index_super_win_components import (
    FeatureDrivenSignalModel,
    InstrumentMap,
)


def test_instrument_map_reads_explicit_bindings():
    mapping = InstrumentMap.from_params(
        {
            "instrument_bindings": [
                {"key": "aapl_main", "signal_symbol": "AAPL", "exec_symbol": "AAPL"},
                {"key": "qqq_levered", "signal_symbol": "QQQ", "exec_symbol": "TQQQ"},
            ]
        }
    )

    assert mapping.signal_symbols() == ["AAPL", "QQQ"]
    assert mapping.binding_for_signal("QQQ").exec_symbol == "TQQQ"


def test_feature_signal_model_uses_notional_to_backfill_qty():
    mapping = InstrumentMap.from_params({"symbols": ["AAPL"]})
    binding = mapping.binding_for_signal("AAPL")
    model = FeatureDrivenSignalModel(default_notional_usd=1000.0)

    decision = model.extract(
        binding=binding,
        features={
            "is_entry_signal": True,
            "intent_side": "LONG",
            "signal_price": 200.0,
            "signal_reason": "breakout",
        },
    )

    assert decision.signal_active is True
    assert decision.target_qty == 5.0
    assert decision.exec_symbol == "AAPL"
    assert decision.reason == "breakout"


def test_feature_signal_model_uses_exec_price_for_mapped_binding():
    mapping = InstrumentMap.from_params(
        {
            "instrument_bindings": [
                {"key": "qqq_levered", "signal_symbol": "QQQ", "exec_symbol": "TQQQ"},
            ]
        }
    )
    binding = mapping.binding_for_signal("QQQ")
    model = FeatureDrivenSignalModel(default_notional_usd=1000.0)

    decision = model.extract(
        binding=binding,
        features={
            "is_entry_signal": True,
            "intent_side": "LONG",
            "signal_price": 500.0,
            "exec_price": 100.0,
            "signal_reason": "levered_breakout",
        },
    )

    assert decision.signal_active is True
    assert decision.target_qty == 10.0
    assert decision.signal_price == 500.0
    assert decision.exec_price == 100.0


def test_feature_signal_model_blocks_notional_backfill_without_exec_price_for_mapped_binding():
    mapping = InstrumentMap.from_params(
        {
            "instrument_bindings": [
                {"key": "qqq_levered", "signal_symbol": "QQQ", "exec_symbol": "TQQQ"},
            ]
        }
    )
    binding = mapping.binding_for_signal("QQQ")
    model = FeatureDrivenSignalModel(default_notional_usd=1000.0)

    decision = model.extract(
        binding=binding,
        features={
            "is_entry_signal": True,
            "intent_side": "LONG",
            "signal_price": 500.0,
            "signal_reason": "levered_breakout",
        },
    )

    assert decision.target_qty == 0.0
    assert "missing_exec_price_for_sizing" in decision.reason


def test_instrument_map_merges_bindings_and_symbol_fallback_without_duplicates():
    mapping = InstrumentMap.from_params(
        {
            "instrument_bindings": [
                {"key": "qqq_levered", "signal_symbol": "QQQ", "exec_symbol": "TQQQ"},
            ],
            "symbols": ["AAPL", "QQQ"],
        }
    )

    assert mapping.signal_symbols() == ["QQQ", "AAPL"]
    assert mapping.binding_for_signal("QQQ").exec_symbol == "TQQQ"
    assert mapping.binding_for_signal("AAPL").exec_symbol == "AAPL"
