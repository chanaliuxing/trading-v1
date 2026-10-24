from __future__ import annotations

from strategies.index_super_win_components import InstrumentMap
from strategies.index_super_win_manifest import ReplicaManifest


def test_replica_manifest_parses_config_only_symbol_rollout() -> None:
    manifest = ReplicaManifest.from_params(
        {
            "replica_manifests": [
                {
                    "key": "qqq_pair",
                    "signal_symbol": "QQQ",
                    "confirm_symbol": "QQEW",
                    "long_exec_symbol": "TQQQ",
                    "short_exec_symbol": "SQQQ",
                    "volatility_symbol": "QQQ",
                    "enabled": True,
                },
                {
                    "key": "aapl_cash",
                    "signal_symbol": "AAPL",
                    "confirm_symbol": "AAPL",
                    "long_exec_symbol": "AAPL",
                    "short_exec_symbol": "AAPL",
                    "volatility_symbol": "AAPL",
                    "enabled": False,
                },
            ]
        }
    )

    assert len(manifest.symbols) == 2
    assert manifest.symbols[0].signal_symbol == "QQQ"
    assert manifest.symbols[0].long_exec_symbol == "TQQQ"
    assert manifest.symbols[0].short_exec_symbol == "SQQQ"


def test_instrument_map_uses_manifest_long_and_short_exec_symbols() -> None:
    instrument_map = InstrumentMap.from_params(
        {
            "replica_manifests": [
                {
                    "key": "qqq_pair",
                    "signal_symbol": "QQQ",
                    "confirm_symbol": "QQEW",
                    "long_exec_symbol": "TQQQ",
                    "short_exec_symbol": "SQQQ",
                }
            ]
        }
    )

    binding = instrument_map.binding_for_signal("QQQ")
    assert binding is not None
    assert binding.exec_symbol_for_side("LONG") == "TQQQ"
    assert binding.exec_symbol_for_side("SHORT") == "SQQQ"
    assert instrument_map.signal_symbols() == ["QQQ"]
    assert instrument_map.exec_symbols() == ["TQQQ", "SQQQ"]
