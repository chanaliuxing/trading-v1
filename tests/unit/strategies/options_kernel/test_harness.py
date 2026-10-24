from pathlib import Path

from strategies.options_kernel.harness import run_direct_entry_parity_scenario_sync


def test_direct_entry_parity_harness_matches_legacy_and_v2(tmp_path: Path):
    result = run_direct_entry_parity_scenario_sync(
        {
            "now": "2026-03-25T11:11:00-04:00",
            "side": "PUT",
            "spot": 588.62,
            "entry_bar": {"vwap": 589.10, "atr_1m": 0.62},
            "plan": {
                "signal_kind": "trend_continuation_put",
                "structure_stop": 589.8,
                "target_1r": 587.9,
                "target_1p2r": 587.5,
                "r_per_share": 0.8,
                "initial_contracts": 4,
            },
            "quote": {
                "opt_symbol": "OPT:QQQ:20260326:P:588",
                "strike": 588.0,
                "bid": 3.90,
                "ask": 4.02,
                "mid": 3.96,
                "last": 3.98,
                "quote_age_sec": 12.0,
            },
            "audit_ids": ["t-fixed", "d-fixed", "x-fixed"],
            "snapshot_root": str(tmp_path / "snapshots"),
        }
    )
    assert result["matched"] is True
    assert result["diff"]["matched"] is True
    assert result["new_kernel"]["ledger"]["lots"]
