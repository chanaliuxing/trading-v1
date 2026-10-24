"""
Simple single-symbol backtest runner for testing visualization.
"""

import json
from datetime import datetime
from backtest_ml_engine import BacktestEngine, BacktestConfig


def run_single_backtest():
    """Run a single backtest for TSLA with ultra-relaxed parameters."""

    # Ultra-relaxed parameters to ensure we get some trades
    strategy_params = {
        "premarket_calibration": {
            "use_config_values": True,  # Use inline params directly
        },
        "A1_thr_sigma": 0.002,  # Very relaxed from 0.006
        "A1_rvol_lo": 0.3,      # Very relaxed from 0.5
        "A1_rvol_hi": 3.0,
        "A1_pos_scale": 1.0,
        "A1_time_exit_min": 5,  # Quick exits
        "A2_grid_g": 0.3,       # Very relaxed from 0.4
        "A2_grid_g_floor": 0.25,
        "A2_pos_scale": 1.0,
        "A2_oneway_thr": 1.0,   # Relaxed from 2.0
        "A2_max_layers": 4,
        "B1_orb_len": 30,
        "B1_rvol_min": 0.3,     # Very relaxed from 0.5
        "B1_pos_scale": 1.0,
        "B1_pullback_window_M": 5,
        "B1_extend_orb": False,
        "windows": {"short_days": 7},
        "k_atr": 1.4,
        "regime_epsilon": 0.005,
        "edge_floor": -1.0,     # Allow very negative edge
        "commission_per_share": 0.0035,
    }

    config = BacktestConfig(
        symbols=["TSLA"],
        start="2025-10-10 09:30:00",
        end="2025-10-14 16:00:00",
        timeframe="1m",
        features_table="trading.features_1m",
        strategy_path="strategies.tsla_v7_2.TSLASimplifiedV7_2",
        strategy_params=strategy_params,
        initial_cash=100000,
        commission_per_share=0.0035,
        commission_min=1.0,
        slippage_bps=1.0,
        fill_ref="open",
    )

    print(f"Running backtest for TSLA from {config.start} to {config.end}...")
    print(f"Ultra-relaxed parameters: A1_thr_sigma={strategy_params['A1_thr_sigma']}, "
          f"A1_rvol_lo={strategy_params['A1_rvol_lo']}, A2_grid_g={strategy_params['A2_grid_g']}")

    engine = BacktestEngine(config)
    results = engine.run()

    # Save results
    output_file = "results/test_single_tsla.json"
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\nBacktest complete!")
    print(f"Results saved to: {output_file}")
    print(f"Total trades: {results.get('metrics', {}).get('trades', 0)}")
    print(f"Final NAV: ${results.get('metrics', {}).get('final_nav', 0):,.2f}")
    print(f"Return: {results.get('metrics', {}).get('return_pct', 0):.2f}%")

    return results


if __name__ == "__main__":
    results = run_single_backtest()
